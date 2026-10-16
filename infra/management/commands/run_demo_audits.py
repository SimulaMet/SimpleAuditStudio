"""Repeatable live-run driver for the agentic demo experiment.

Creates audit runs through the running Studio's REST API (token auth, same
endpoints the web app uses), waits for each run to finish, extracts verdict /
findings / judge / span evidence per scenario, and records the results two ways:

  * one JSONL line per run in ``results/demo_runs/`` (machine-readable), and
  * a row appended to the markdown scorecard table in
    ``docs/agentic-demo-results.md`` (human-readable).

It works against any reachable stack (the dev server by default):

    uv run manage.py run_demo_audits --scenario-key a01 --repetitions 3
    uv run manage.py run_demo_audits --scenario-keys a01,a03 --base-url http://localhost:8001

Model/agent/judge configuration is resolved from the most recent existing run
in the target workspace (so the harness reuses the exact frozen configuration a
live demo already used) unless overridden with --target-model-id /
--auditor-model-id / --judge-model-id / --agent-id.
"""
from __future__ import annotations

import http.cookiejar
import json
import logging
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

logger = logging.getLogger(__name__)

RESULTS_DIR = Path(__file__).resolve().parents[4] / "results" / "demo_runs"
SCORECARD_PATH = Path(__file__).resolve().parents[4] / "docs" / "agentic-demo-results.md"
TERMINAL_RUN_STATUSES = {"completed", "failed", "cancelled"}


class StudioClient:
    """Minimal token-auth HTTP client for the Studio REST API.

    Keeps a cookie jar so mutating endpoints (publish / create) can send the
    ``X-CSRFToken`` header Django's CsrfViewMiddleware requires alongside the
    token.
    """

    def __init__(self, base_url: str, username: str, password: str, timeout: float = 15.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.token = ""
        self._jar = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self._jar))
        self._auth(username, password)

    def _request(self, method: str, path: str, payload: dict | None = None) -> dict | list:
        url = f"{self.base_url}{path}"
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("Authorization", f"Token {self.token}")
        csrf = next((c.value for c in self._jar if c.name == "csrftoken"), None)
        if csrf and method.upper() != "GET":
            req.add_header("X-CSRFToken", csrf)
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                body = resp.read().decode()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:500]
            raise CommandError(f"{method} {path} -> {exc.code}: {detail}") from exc
        return json.loads(body) if body.strip() else {}

    def _csrf_cookie(self) -> None:
        """Prime the csrftoken cookie by visiting any authenticated page once."""
        try:
            self._opener.open(f"{self.base_url}/scenarios/", timeout=self.timeout)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            # Best-effort priming only; authenticated POSTs surface real errors.
            logger.debug("CSRF cookie priming visit failed: %s", exc)

    def _auth(self, username: str, password: str) -> None:
        self._csrf_cookie()
        body = self._request("POST", "/api/auth/token/", {"username": username, "password": password})
        self.token = body.get("token", "")
        if not self.token:
            raise CommandError("Authentication did not return a token.")
        self._csrf_cookie()

    # ── Domain helpers ────────────────────────────────────────────────
    def workspaces(self) -> list[dict]:
        return self._request("GET", "/api/projects/")

    def scenarios(self, project_id: int) -> list[dict]:
        return self._request("GET", f"/api/projects/{project_id}/scenarios/")

    def scenario_sets(self, project_id: int) -> list[dict]:
        return self._request("GET", f"/api/projects/{project_id}/scenario-sets/")

    def set_versions(self, project_id: int, set_id: int) -> list[dict]:
        return self._request("GET", f"/api/projects/{project_id}/scenario-sets/{set_id}/versions/")

    def agents(self) -> list[dict]:
        return self._request("GET", "/api/agents/")

    def latest_run(self, project_id: int) -> dict | None:
        runs = self._request("GET", f"/api/projects/{project_id}/audit-runs/")
        if not runs:
            return None
        return max(runs, key=lambda r: r.get("created_at") or "")

    def create_run(self, project_id: int, payload: dict) -> dict:
        return self._request("POST", f"/api/projects/{project_id}/audit-runs/create/", payload)

    def get_run(self, project_id: int, run_id: int) -> dict:
        return self._request("GET", f"/api/projects/{project_id}/audit-runs/{run_id}/")

    def run_results(self, project_id: int, run_id: int) -> list[dict]:
        return self._request("GET", f"/api/projects/{project_id}/audit-runs/{run_id}/results/")


def _extract_scenario_record(result_row: dict) -> dict:
    """Pull verdict / findings / judge / span evidence out of one result row."""
    result = result_row.get("result") or {}
    reps = result.get("reps") if isinstance(result.get("reps"), list) else [result]
    rep = next((r for r in reps if isinstance(r, dict)), {})
    ag = rep.get("agentic_evaluation") if isinstance(rep.get("agentic_evaluation"), dict) else {}
    judgment = rep.get("judgment") if isinstance(rep.get("judgment"), dict) else {}
    spans = judgment.get("evidence_spans") if isinstance(judgment.get("evidence_spans"), list) else []
    checks = ag.get("checks") if isinstance(ag.get("checks"), list) else []
    semantic = ag.get("semantic_judgement") or ag.get("semantic")
    failed_checks = [c for c in checks if isinstance(c, dict) and str(c.get("status")).upper() == "FAIL"]
    inconclusive = [c for c in checks if isinstance(c, dict) and str(c.get("status")).upper() == "INCONCLUSIVE"]
    return {
        "version_item_id": result_row.get("version_item_id"),
        "scenario_key": result_row.get("scenario_key"),
        "scenario_title": result_row.get("scenario_title"),
        "status": result_row.get("status"),
        "severity": rep.get("severity") or result.get("severity"),
        "verdict": str((ag.get("verdict") or {}).get("status") or ag.get("status") or "").upper(),
        "verdict_reason": str((ag.get("verdict") or {}).get("reason") or ag.get("reason") or ""),
        "n_checks": len(checks),
        "n_failed": len(failed_checks),
        "n_inconclusive": len(inconclusive),
        "failed_checks": [
            {"id": c.get("id"), "severity": c.get("severity"), "summary": c.get("summary")}
            for c in failed_checks
        ],
        "inconclusive_checks": [c.get("id") for c in inconclusive],
        "span_refs": [
            {"span_id": s.get("span_id"), "kind": s.get("kind"), "name": s.get("name")}
            for s in spans
            if isinstance(s, dict) and s.get("span_id")
        ],
        "trace_ids": rep.get("trace_ids") or [],
        "semantic_status": str(semantic.get("status", "")).upper() if isinstance(semantic, dict) else "",
        "semantic_model_version": str(semantic.get("model_version", "")) if isinstance(semantic, dict) else "",
        "semantic_prompt_version": str(semantic.get("prompt_version", "")) if isinstance(semantic, dict) else "",
    }


def _config_from_latest_run(latest: dict) -> dict:
    """Recover the model/agent/judge ids a previous run used, for reuse."""
    cfg: dict = {}
    target = latest.get("target_config_snapshot") or {}
    if target.get("id"):
        cfg["target_model_id"] = target["id"]
    if latest.get("agent"):
        cfg["agent_id"] = latest["agent"]
    auditor = latest.get("auditor_config_snapshot") or {}
    if auditor.get("id"):
        cfg["auditor_model_id"] = auditor["id"]
    judge = latest.get("judge_config_snapshot") or {}
    if isinstance(judge, dict) and judge.get("judge_id"):
        cfg["judge_id"] = judge["judge_id"]
    elif judge.get("id"):
        cfg["judge_model_id"] = judge["id"]
    return cfg


SCORECARD_HEADER = (
    "| scenario | run_id | revision | target/judge config | verdict | expected | observed | span evidence refs | annotation | fixes |\n"
    "|---|---|---|---|---|---|---|---|---|---|\n"
)


def _scorecard_file_ready(path: Path) -> bool:
    if path.exists():
        return True
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "# Agentic demo — live run scorecard\n\n"
        "Machine-appended by `manage.py run_demo_audits`. One row per live run; annotate "
        "`annotation`/`fixes` by hand after reviewing each run.\n\n" + SCORECARD_HEADER,
        encoding="utf-8",
    )
    return True


def _md_cell(value) -> str:
    return str(value if value is not None else "—").replace("|", "\\|").replace("\n", " ")


class Command(BaseCommand):
    help = "Run live agentic demo audits via the REST API and record a scorecard."

    def add_arguments(self, parser):
        parser.add_argument("--scenario-keys", dest="scenario_keys", default="",
                            help="Comma-separated scenario keys to run (e.g. a01,a03). Required.")
        parser.add_argument("--scenario-key", dest="scenario_key", default="",
                            help="Single scenario key (alternative to --scenario-keys).")
        parser.add_argument("--repetitions", type=int, default=1,
                            help="n_repetitions for the run (default 1).")
        parser.add_argument("--max-turns", type=int, default=1,
                            help="max_turns for the run (default 1 for single-turn scenarios).")
        parser.add_argument("--base-url", default="http://localhost:8000",
                            help="Studio base URL (default http://localhost:8000).")
        parser.add_argument("--username", default="studio", help="API username (default studio).")
        parser.add_argument("--password", default="localdevpass123", help="API password.")
        parser.add_argument("--project-id", type=int, default=None,
                            help="Workspace id (default: first workspace).")
        parser.add_argument("--run-name", default=None, help="Override the run name.")
        parser.add_argument("--target-model-id", type=int, default=None)
        parser.add_argument("--auditor-model-id", type=int, default=None)
        parser.add_argument("--judge-model-id", type=int, default=None)
        parser.add_argument("--judge-id", type=int, default=None)
        parser.add_argument("--agent-id", type=int, default=None)
        parser.add_argument("--timeout-seconds", type=float, default=600.0,
                            help="How long to wait for the run to finish (default 600s).")
        parser.add_argument("--poll-seconds", type=float, default=5.0,
                            help="Poll interval while waiting (default 5s).")
        parser.add_argument("--dry-run", action="store_true",
                            help="Resolve everything and print the plan without creating a run.")
        parser.add_argument("--reuse-version", action="store_true",
                            help="Reuse an already-published set version that covers "
                                 "these scenarios instead of publishing a fresh one. "
                                 "Off by default so the harness always audits the "
                                 "current seeded metadata (a stale single-scenario "
                                 "version must not shadow a newer full-pack revision).")

    def handle(self, *args, **options):
        keys = [k.strip() for k in (options["scenario_keys"] or options["scenario_key"]).split(",") if k.strip()]
        if not keys:
            raise CommandError("Provide --scenario-keys (comma-separated) or --scenario-key.")
        n_reps = options["repetitions"]
        max_turns = options["max_turns"]

        client = StudioClient(options["base_url"], options["username"], options["password"])
        self.stderr.write(f"Authenticated against {client.base_url}")

        workspaces = client.workspaces()
        if not workspaces:
            raise CommandError("No workspaces found.")
        project_id = options["project_id"] or workspaces[0]["id"]

        scenarios = client.scenarios(project_id)
        by_key = {s["key"]: s for s in scenarios}
        missing = [k for k in keys if k not in by_key]
        if missing:
            raise CommandError(f"Scenario keys not found in workspace {project_id}: {', '.join(missing)}")

        # Resolve the scenario set version to audit. By default we publish a
        # FRESH immutable version from the selected scenario ids so the harness
        # always runs against current seeded metadata — an older single-scenario
        # version left over from a previous fallback publish must not shadow a
        # newer full-pack revision. With --reuse-version we instead pick the
        # highest already-published version that covers exactly these ids.
        set_version = None
        wanted_ids = sorted(by_key[k]["id"] for k in keys)
        if options["reuse_version"]:
            for s in client.scenario_sets(project_id):
                for v in client.set_versions(project_id, s["id"]):
                    v_ids = sorted(i["scenario_id"] for i in v.get("items", []))
                    if v_ids == wanted_ids and (
                        set_version is None
                        or int(v.get("version", 0)) > int(set_version.get("version", 0))
                    ):
                        set_version = v
        if set_version is None:
            set_id = None
            for s in client.scenario_sets(project_id):
                if "Explanatory" in (s.get("name") or ""):
                    set_id = s["id"]
                    break
            if set_id is None:
                for s in client.scenario_sets(project_id):
                    if "Agentic" in (s.get("name") or ""):
                        set_id = s["id"]
                        break
            if set_id is None and client.scenario_sets(project_id):
                set_id = client.scenario_sets(project_id)[0]["id"]
            if set_id is None:
                raise CommandError("No scenario set available to publish from.")
            set_version = client._request(
                "POST", f"/api/projects/{project_id}/scenario-sets/{set_id}/publish/",
                {"scenario_ids": wanted_ids},
            )
            self.stderr.write(f"Published new set version {set_version['id']} (v{set_version['version']}).")

        # Configuration: explicit flags win; otherwise reuse the latest run's.
        latest = client.latest_run(project_id)
        cfg = _config_from_latest_run(latest or {})
        for flag, field in (
            ("target_model_id", "target_model_id"),
            ("auditor_model_id", "auditor_model_id"),
            ("judge_model_id", "judge_model_id"),
            ("judge_id", "judge_id"),
            ("agent_id", "agent_id"),
        ):
            if options[flag] is not None:
                cfg[field] = options[flag]
        # An explicit target model means the caller wants a bare-model target;
        # drop any inherited agent so it doesn't override the target.
        if options["target_model_id"] is not None and options["agent_id"] is None:
            cfg.pop("agent_id", None)
        # A bare-model fallback when no agent was configured before.
        if not cfg.get("agent_id") and not cfg.get("target_model_id"):
            agents = client.agents()
            if agents:
                cfg["agent_id"] = agents[0]["id"]
                self.stderr.write(f"No prior run config; defaulting to agent '{agents[0]['name']}' (id {cfg['agent_id']}).")
        # Judge: the create endpoint wants either judge_id or judge_model_id.
        # A prior run's judge snapshot gives us both (a registered model id plus
        # the judge it graded with); prefer the explicit judge id.
        if not cfg.get("judge_id") and not cfg.get("judge_model_id"):
            judge_snap = (latest or {}).get("judge_config_snapshot") or {}
            if isinstance(judge_snap, dict):
                if judge_snap.get("judge", {}).get("judge_id"):
                    cfg["judge_id"] = judge_snap["judge"]["judge_id"]
                elif judge_snap.get("id"):
                    cfg["judge_model_id"] = judge_snap["id"]
        if not cfg.get("target_model_id"):
            raise CommandError("Could not resolve a target model. Pass --target-model-id (or --agent-id).")
        if not cfg.get("auditor_model_id"):
            raise CommandError("Could not resolve an auditor model. Pass --auditor-model-id.")
        if not cfg.get("judge_id") and not cfg.get("judge_model_id"):
            raise CommandError("Could not resolve a judge. Pass --judge-id or --judge-model-id.")

        run_name = options["run_name"] or f"Demo: {len(keys)} scenarios ({n_reps}x)"
        payload = {
            "name": run_name,
            "scenario_set_version_id": set_version["id"],
            "target_model_id": cfg["target_model_id"],
            "auditor_model_id": cfg["auditor_model_id"],
            "max_turns": max_turns,
            "n_repetitions": n_reps,
        }
        if cfg.get("agent_id"):
            payload["agent_id"] = cfg["agent_id"]
        # The create endpoint requires judge_model_id; judge_id selects the
        # grading rubric on top of it. Send both when known.
        if cfg.get("judge_id"):
            payload["judge_id"] = cfg["judge_id"]
        if cfg.get("judge_model_id"):
            payload["judge_model_id"] = cfg["judge_model_id"]

        if options["dry_run"]:
            self.stdout.write(json.dumps({
                "base_url": client.base_url, "project_id": project_id,
                "scenario_keys": keys, "set_version": set_version["id"],
                "revision": set_version.get("version"),
                "content_hash": set_version.get("content_hash"),
                "run_payload": payload,
            }, indent=2))
            self.stderr.write("Dry run: nothing created.")
            return

        run = client.create_run(project_id, payload)
        run_id = run["id"]
        self.stderr.write(f"Created run #{run_id}; waiting up to {options['timeout_seconds']:.0f}s ...")

        deadline = time.time() + options["timeout_seconds"]
        status = run.get("status")
        while status not in TERMINAL_RUN_STATUSES:
            if time.time() > deadline:
                raise CommandError(f"Run #{run_id} still {status} after {options['timeout_seconds']:.0f}s; aborting (it keeps running server-side).")
            time.sleep(options["poll_seconds"])
            status = client.get_run(project_id, run_id).get("status")
        self.stderr.write(f"Run #{run_id} finished with status={status}.")

        rows = [_extract_scenario_record(r) for r in client.run_results(project_id, run_id)]
        revisions = {i["scenario_key"]: i.get("revision") for i in set_version.get("items", [])}
        target_snap = run.get("target_config_snapshot") or {}
        judge_snap = run.get("judge_config_snapshot") or {}
        config_label = (
            f"target={target_snap.get('display_name') or cfg.get('agent_id')} "
            f"judge={(judge_snap.get('name') or judge_snap.get('judge_id'))}"
        )

        record = {
            "recorded_at": datetime.now(UTC).isoformat(),
            "base_url": client.base_url,
            "project_id": project_id,
            "run_id": run_id,
            "run_name": run_name,
            "status": status,
            "revision": set_version.get("version"),
            "content_hash": set_version.get("content_hash"),
            "max_turns": max_turns,
            "n_repetitions": n_reps,
            "config": config_label,
            "scenarios": rows,
        }

        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        jsonl_path = RESULTS_DIR / f"demo_runs_{datetime.now(UTC):%Y%m%d}.jsonl"
        with jsonl_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        self.stderr.write(f"Appended JSONL record to {jsonl_path}")

        _scorecard_file_ready(SCORECARD_PATH)
        new_rows = []
        for row in rows:
            verdict = row["verdict"] or row["severity"] or "—"
            observed = ", ".join(f"{c['id']}:{c['severity']}" for c in row["failed_checks"]) or (
                "no failures" if row["n_failed"] == 0 else ""
            )
            if row["n_inconclusive"]:
                observed = (observed + "; " if observed else "") + f"inconclusive: {', '.join(row['inconclusive_checks'])}"
            spans = ", ".join(f"{s['kind']}:{s['span_id']}" for s in row["span_refs"][:6]) or "—"
            new_rows.append(
                "| " + " | ".join([
                    _md_cell(f"{row['scenario_key']} — {row['scenario_title']}"),
                    _md_cell(run_id),
                    _md_cell(revisions.get(row["scenario_key"])),
                    _md_cell(config_label),
                    _md_cell(verdict),
                    _md_cell("see scenario"),
                    _md_cell(observed),
                    _md_cell(spans),
                    "",  # annotation (manual)
                    "",  # fixes (manual)
                ]) + " |"
            )
        with SCORECARD_PATH.open("a", encoding="utf-8") as fh:
            fh.write("\n".join(new_rows) + "\n")
        self.stderr.write(f"Appended {len(new_rows)} row(s) to {SCORECARD_PATH}")

        self.stdout.write(json.dumps(record, indent=2, ensure_ascii=False))
