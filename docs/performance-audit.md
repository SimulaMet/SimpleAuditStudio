# Performance audit (2026-11-11)

Source: read-only code review by three parallel audits (web views, data layer, API/SSE/worker/deploy). Nothing here is measured. Confirm against Sentry or production timings before and after each fix. Trigger: `502` on `GET /runs/data/?page=1&size=25&sort=created_at&dir=desc` on a production deployment.

## Production evidence (Sentry, project `simula-l0/simpleauditstudio`, last 14 days)

Queried with `sentry explore --dataset spans`. Traces are sampled at 10%, so counts are estimates; durations are real.

Slowest web transactions (excluding `chat <model>` transactions, which are model calls made by the audit worker, not web pages):

| Transaction | p50 | p95 | max |
|---|---|---|---|
| `/experiments/{experiment_id}/` | 206 ms | 9,276 ms | 10,376 ms |
| `/experiments/new/` | 170 ms | 3,432 ms | 3,435 ms |
| `/runs/data/` | 115 ms | 3,058 ms | 3,449 ms |
| `/scenarios/{set_id}/import/` | 1,370 ms | 1,520 ms | 1,520 ms |
| `/agents/{agent_id}/` | 711 ms | 1,321 ms | 1,321 ms |
| `/chat/authz` (about 34k calls) | 101 ms | 472 ms | 2,855 ms |
| `/otlp/v1/traces` | 8 ms | 100 ms | 183,294 ms |

Root cause confirmed in span data: the `SELECT ... FROM core_scenario_result WHERE run_id IN (...)` query from `pass_counts` (it selects the `result` column). On `/runs/data/` it has p95 2.2 s and accounts for about half of all DB time. On `/experiments/{id}/` it runs 100 + 260 times with p95 3.3 s and 1.9 s and accounts for about 464 s of 486 s total DB time. The experiment page is therefore dominated by this one query pattern.

Unresolved Sentry issues relevant to this audit:
- `OperationalError: connection timeout expired` on `/otlp/v1/traces` (SIMPLEAUDITSTUDIO-33, -34) and `/experiments/` (-32): DB connection exhaustion or contention, consistent with the thread starvation finding below.
- `SystemExit: 1` in `audits.views.event_stream` (SIMPLEAUDITSTUDIO-W, 65 events): SSE worker being killed.
- Chat model-call errors (`gpt-6-luna` content filter, `nhi.short-answer-active` 502 from NEL search provider) come from audited targets, not Studio pages.

## Status (branch `perf/scenario-result-summary-columns`)

Done:
- `ScenarioResult` trial-count columns and `list_summary` projection (migration 0011, batched backfill). `pass_counts` is one `GROUP BY`; the run Results list (`_result_rows`) no longer reads transcripts.
- Index migrations: `AuditRun` (project/archived/created_at, status/archived, monitor/created_at, started_at), `AuditEvent` (dropped three single-column indexes, added run/kind/item), `ScenarioResult(run_id, status)`, `OtlpSpan` (received_at, target/received_at). `audits` 0012, `model_registry` 0023.
- Quick wins: removed the Python scan of all events in the fault-injection path, capped the JSON event poll at 1000, `delete_prompt_version` compares in the database, cached the timezone list and git commit, queue health probe is two aggregate queries, runs grid and CSV defer unused JSON columns.
- SSE: poll backs off from 1 s to 5 s while idle, `: keepalive` comment every 15 s.
- `/chat/authz`: only re-stamps the embed-admin session flag when under half the TTL remains.
- `/static/`: `Cache-Control: public, max-age=300`. Caddyfile: `encode zstd gzip` and upstream dial/response timeouts (not validated, `caddy` is not installed locally).

Also done in the second pass:
- Worker: `_sync_run_counters` is one conditional aggregate; `_maybe_finalize` reads two columns until the run is complete.
- SSE: a finished run whose terminal event is missing now ends the stream (after two quiet polls) with a synthetic `run_<status>` event instead of idling for an hour.
- Model discovery and the health model-endpoint probe ping concurrently (8 threads) instead of serially.
- `nav` and `admin_status` share one `is_any_project_admin` lookup per request.
- Experiment page reload interval 15 s -> 30 s; `/experiments/new/` focus refetch limited to once a minute.

Done in the third pass:
- Middleware and context processors: one `{project_id: role}` query per request (`accounts.services.request_roles`) feeds workspace access, the admin flag, the workspace switcher and `can_write`. Token-authenticated requests no longer write or cycle a session.
- SSE caps per web process: 3 streams per user and half of `GUNICORN_THREADS` in total (`SIMPLEAUDIT_SSE_MAX_STREAMS_PER_USER`, `SIMPLEAUDIT_SSE_MAX_STREAMS`); over the cap the stream answers 429 and the browser retries. The slot is released when Django closes the response.
- `AuditEvent`: the worker flusher writes queued turn events with one `bulk_create` per batch (up to 50); the sweeper deletes `scenario_turn` / `scenario_rep_started` events of runs finished more than `SIMPLEAUDIT_EVENT_RETENTION_DAYS` (30) ago, hourly; `manage.py prune_audit_events` does it by hand.
- API pagination: `/api/projects/<id>/audit-runs/` (default 50, max 200, newest first) and `.../results/` (default 100, max 500) answer `{count, next, previous, results}`. `run_demo_audits` follows pages.
- Agent page: the GET-time Open WebUI sync runs at most once per `SIMPLEAUDIT_OPENWEBUI_SYNC_TTL_S` (60) per user and workspace; its lists double as the ACL check, so a load makes 2 list calls instead of 4; mirrored rows are read in one query and only saved when changed. The Sync button always runs.
- Scenarios page: each edit form is fetched on first "Edit" click (`/scenarios/edit-form/<item>/`) instead of being rendered for every scenario.
- Compare uses the `list_summary` projection and loads each pinned version's items once; its run picker reads only the shown columns; experiment pages defer `runtime_metadata` and `trace_config`.

Done in the fourth pass:
- Exports stream: the run page JSON/CSV download (CSV reads only the `list_summary` projection; JSON reads results in chunks of 100), the dashboard runs CSV (batches of 500 runs, pass counts per batch), and the API engine export (built whole because the engine computes its summary over every result, then encoded and sent incrementally).
- Run list API rows are slim (`AuditRunListSerializer`: no config snapshots, adds frozen target/judge names and pass rate); `?expand=snapshots` returns the old full rows.

Remaining idea, not started:
- Serving SSE from an async worker so streams use no threads at all.

## Tier 1: likely causes of 502s and slow pages

1. **`ScenarioResult.result` loaded in full everywhere.** Severity and counts are read out of full transcript JSON in:
   - `pass_counts` (`audits/monitors.py:449`), called from `infra/runs_table.py` (`/runs/data/`, CSV), `infra/ui.py` (ExperimentsView), `audits/experiments.py`, `audits/monitors.py` (drift series).
   - `_result_rows` (`infra/ui.py:3477`): run detail and `/runs/<id>/results-fragment/`. `templates/partials/run_detail_body.html` refetches the fragment on every `scenario_completed` event, so a run with N scenarios loads N blobs N times.
   - Exports (`RunExportView`, `build_simpleaudit_export`, export-html), `compare_runs`, `_refresh_run_summary_metrics`.
   - Fix: denormalize small columns at write time (`upsert_scenario_result`): `severity`, `aggregated_severity`, `agreement_rate`, and per-row trial counts (`n_pass`, `n_judged`, `n_errors`, `n_ungraded`). `pass_counts` becomes one `GROUP BY run_id` with `Sum`. Needs a migration with a batched backfill. Portable across SQLite and Postgres (the `reps` array cannot be aggregated portably in the ORM, hence the columns).
2. **SSE streams starve the thread pool** (`audits/views.py:291-318`, `gunicorn.conf.py`). Each viewer holds one of 32 gthread threads for up to 1h and polls the DB every second. Static files, `/chat/authz` and `/healthz` use the same threads. The loop never checks `run.status`; there is no heartbeat. Fix: per-user cap, poll backoff, end on terminal status, heartbeat plus `retry`, serve SSE from an async or separate service.
3. **Slow outbound calls in web requests vs 60s gunicorn timeout.** `DiscoverModelsView` (sequential probes, 15s each), OTLP credential test (60s), `collect_health` (serial probes), `AgentDetailView` GET (4-6 Open WebUI calls plus row writes). Fix: thread pools, short timeouts, caching, move Agent sync off GET.
4. **`/chat/authz` on every Open WebUI asset.** Full Django stack with about 10 queries per request. Fix: cache per session for 30-60s, light path that skips ProjectMiddleware, skip forward-auth for public assets in Caddy, skip the admin query for superusers, avoid session writes on every embed call.
5. **Middleware and context processors.** About 10-14 queries on every page and poll (session, user, project, membership, admin checks, workspaces, write access, site settings). No `CACHES`, DB-backed sessions. Fix: resolve role once per request, add `CACHES`, `cached_db` sessions, cache `get_site_setting`. `TokenSessionBridgeMiddleware` creates a session per API request; skip it for `/api/`.

## Tier 2: indexes and unbounded queries

- `AuditRun`: add `(project, archived, -created_at)`, `(status, archived)` (partial on non-terminal for the sweeper on Postgres), `(monitor, created_at)`, `started_at`.
- `AuditEvent`: drop single-column `run_id`, `version_item_id`, `kind` indexes; add `(run_id, kind, version_item_id)`; add retention for finished runs; batch turn-event inserts with `bulk_create`; make `progress_snapshot` O(1) or cheaper.
- `ScenarioResult`: add `(run_id, status, attempts)`; collapse the 3-4 per-scenario COUNT queries in `_sync_run_counters` / `_maybe_finalize` into one conditional aggregate.
- `OtlpSpan`: index `received_at`; drop redundant `target_id` index; move pruning to the sweeper.
- Unbounded list APIs: `/api/projects/<id>/audit-runs/` (all snapshot JSON), results API, `poll_audit_run_events`, `list_scenarios`, `list_versions`.
- `.defer()` unused JSON columns on the runs grid and CSV (CSV loads up to 10,000 full rows); stream CSV/JSON exports with `.iterator()`.
- N+1: `ScenarioListSerializer.get_latest_revision`, `export_scenarios`, `NewExperimentView` connections, judge pickers, scenario publish/import loops, `RunDetailView` frozen-model FKs.
- Page weight: experiment detail does `location.reload()` every 15s; scenarios page renders every edit form eagerly; `/experiments/new/` refetches on every window focus; result detail page ships the blob twice.
- Python-side scans: `delete_prompt_version` (compare snapshots in Python), `_iter_attempted_events` (fault-injection path).
- Transactions holding network calls: `run_due_monitors` and `create_audit_run` call `discover_target_metadata` inside atomic blocks.

## Tier 3: deploy and config

- Compression: `encode gzip zstd` in Caddy (not Django gzip, which buffers SSE).
- Static files: whitenoise or Caddy `file_server` with cache headers; currently served by Django through the same 32 threads.
- Caddy upstream: add dial and response-header timeouts, health checks.
- Gunicorn `timeout=60` is shorter than several slow endpoints.
- Compose `web` runs migrate, bootstrap, seed and collectstatic on every start; split into an init step.
- Sentry `enable_logs=True` ships every log line; use `traces_sampler` for hot routes.
- `readyz` caches a "pending migrations" True forever via `lru_cache`.

## Other issues noticed

- `next_revision` / `next_version` use `count()+1`; collides after a delete. Use `Max()+1`.
- `VisualizerImageView` fetches arbitrary URLs (SSRF, no size limit).
- Public experiment pages and `/sitemap.xml` are anonymous and uncached.
- `Meta.ordering` on joined fields (`project__name`, `connection__name`, ...) adds joins to `.first()` / `.get()`.

## Suggested order

1. Tier 2 indexes, `limit`s and `.defer()` quick wins.
2. Tier 1 item 1 (`ScenarioResult` summary columns).
3. Tier 1 items 2-5 and Tier 3.

## Not covered in depth

`infra/engine.py`, `infra/seed.py`, `audits/agentic/*`, Agent/KB/Tool query paths beyond list views, migrations other than an index grep.
