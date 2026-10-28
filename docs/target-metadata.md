# Optional target metadata in model discovery

SimpleAuditStudio supports an **optional, nonstandard OpenAI-compatible extension**
for targets that want to expose auditable configuration before an audit begins.
The official OpenAI Model object does **not** define `metadata` on `/v1/models`.
Do not apply the unrelated 16 string-pair/512-character constraint of OpenAI's
shared Metadata type to this extension.

## 1. How to expose metadata from your target

Return a JSON dictionary under `metadata` on either of these endpoints (both are
tried; detail first, list as fallback):

| Endpoint | Where the dict lives |
|---|---|
| `GET /v1/models/{model_id}` | top-level `metadata` key, or `data.metadata` if wrapped in `{ "data": … }` |
| `GET /v1/models` | `metadata` key inside the matching entry of `data[]` / `models[]` |

Open WebUI's `info.meta` shape is also recognised on both endpoints:

```json
{ "id": "my-agent", "info": { "meta": { "description": "…" } } }
```

Example — model detail response (`GET /v1/models/my-rag-agent`):

```json
{
  "id": "my-rag-agent",
  "object": "model",
  "metadata": {
    "version": "2.3.1",
    "framework": "langgraph",
    "prompts": {
      "system":    { "content": "You are a helpful assistant.", "version": "3.2" },
      "retrieval": { "content": "Retrieve relevant documents for {query}", "version": "1.4" }
    },
    "retrieval":  { "strategy": "hybrid", "top_k": 5 },
    "tools":      ["search", "calculator"],
    "guardrails": { "input": ["prompt_injection"] }
  }
}
```

The schema inside `metadata` is **open**: any keys and nested JSON values are
preserved, including unfamiliar ones. `prompts`, `framework`, `version`,
`retrieval`, `tools`, `guardrails`, etc. are conventions, not required fields.

### Limits and safety

| Constraint | Value | Rationale |
|---|---|---|
| Max serialised size | 256 KiB | Prevents pathological payloads |
| Max nesting depth | 24 levels | Guards against stack overflow |
| Secret-key redaction | keys matching `password`, `api_key`, `access_token`, `secret_reference`, `*_token`, `*_secret`, `*_password`, `*_api_key` → `[REDACTED]` | Avoids leaking credentials into the DB |
| Discovery timeout | 2 s per request | Never blocks an audit |
| Network failure | treated as `unavailable`; audit proceeds | Metadata is optional |

> **Note:** the snapshot is a *target-declared claim*, not independent proof of
> which prompts, guardrails, or tools were actually used. Prompt bodies can
> still contain secrets even after key-level redaction, so curate what you
> expose.

## 2. How it is stored

Discovery runs **once** when a new `AuditRun` is created (in
`audits/services.py`). The result is frozen into the run's
`target_config_snapshot` JSON field:

```jsonc
// AuditRun.target_config_snapshot
{
  "remote_metadata": {              // the sanitised metadata dict (or absent)
    "version": "2.3.1",
    "prompts": { "system": { "content": "…", "version": "3.2" } }
  },
  "metadata_discovery": {           // provenance envelope
    "status": "available",          // "available" | "absent" | "unavailable"
    "source": "models.retrieve",   // "models.retrieve" | "models.list" | null
    "captured_at": "2026-10-28T09:00:00+00:00"
  }
  // …other frozen inputs (model IDs, gen config, etc.)
}
```

* **No database migration** — reuses the existing `target_config_snapshot`
  JSONField on `AuditRun`.
* **Retries and historical views** read only the frozen snapshot; the target
  is never re-probed after the initial capture.
* **Resumed runs** keep their original frozen metadata.

## 3. How it is shown in the UI

### Run detail — Frozen Manifest (`/runs/<id>/`)

Below the frozen model cards, a **"Target Metadata"** section appears when
`remote_metadata` is present. Each leaf key becomes one dot-path row:

| Key (dot path) | Value |
|---|---|
| `prompts.system.content` | You are a helpful assistant. |
| `prompts.system.version` | 3.2 |
| `retrieval.top_k` | 5 |
| `tools` | `["search", "calculator"]` *(JSON array stays in one row)* |

* Values longer than 160 characters are truncated to 90 chars with a
  click-to-expand `<details>` block.
* Booleans, numbers, and `null` are rendered as their JSON string form
  (`"true"`, `"5"`, `"null"`).
* Empty objects render as `{}`.

### Compare Runs — Input Parameters table (`/compare/?runs[]=…`)

When two or more runs are compared, the **union** of all frozen metadata keys
across the selected runs appears as extra rows labelled
`Target metadata · <dot.path>`.

* A key present in some runs but not others shows `—` for the missing runs and
  is flagged as **differs**.
* Changed values are highlighted the same way as other parameter diffs.
* Long values get the same expand-on-click treatment.
* No provider calls are made during comparison — everything reads from the
  frozen snapshots.

## 4. Code map

| Concern | File |
|---|---|
| HTTP discovery + sanitisation | `model_registry/remote_metadata.py` |
| Flattening + frozen-snapshot reader | `audits/target_metadata.py` |
| Freeze-on-create hook | `audits/services.py` (in `create_audit_run`) |
| Comparison rows | `audits/comparison.py` (`compare_runs`) |
| Run-detail context | `infra/ui.py` (`ctx["target_metadata_rows"]`) |
| Run-detail template | `templates/partials/run_detail_body.html` |
| Compare template | `templates/compare.html` |
| Unit tests | `model_registry/tests/test_remote_metadata.py`, `infra/tests/test_audit_run_freeze.py`, `infra/tests/test_comparison.py` |
