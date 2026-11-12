"""Fixture contracts and sanitization helpers for the agentic demo.

This module does not turn synthetic data into recorded-run provenance.  A
fixture claiming ``recorded_real_run_fixture`` must carry explicit execution
provenance and pass validation before a future capture command can export it.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

FIXTURE_FORMAT = "simpleaudit-agentic-demo"
FIXTURE_VERSION = 1
#: A key names a secret when it contains one of these words, or "token" as a
#: whole word segment (token, access_token, client-token), not tokenizer_model.
_SECRET_KEY = re.compile(
    r"(api[_-]?key|authorization|cookie|password|secret|(?:^|[_.\-])token(?:$|[_.\-]))", re.IGNORECASE
)
_REDACTED = "[REDACTED]"


class FixtureValidationError(ValueError):
    """Raised when a fixture is incomplete or unsafe to load."""


def export_run_fixture(run: Any) -> dict[str, Any]:
    """Export one completed, explicitly executed DB run as a fixture.

    This is deliberately a refusal-oriented boundary: a preloaded/synthetic
    run cannot acquire recorded provenance merely by being exported.  The
    caller must mark the source run as executed in ``runtime_metadata``.
    """
    from audits.events import ScenarioResult
    from model_registry.models import OtlpSpan

    if getattr(run, "status", None) != "completed":
        raise FixtureValidationError("run must be completed")
    runtime = dict(getattr(run, "runtime_metadata", None) or {})
    if runtime.get("executed") is not True:
        raise FixtureValidationError("run must declare runtime_metadata.executed=true")
    if runtime.get("agentic_demo_seed") or "synthetic" in str(runtime.get("source", "")).lower():
        raise FixtureValidationError("synthetic/preloaded runs cannot be exported as recorded")

    items = list(run.scenario_set_version.items.select_related("scenario", "revision").order_by("position"))
    rows = list(ScenarioResult.objects.filter(run_id=run.pk))
    by_item = {str(row.version_item_id): row for row in rows}
    if len(by_item) != len(items):
        raise FixtureValidationError("run does not have a result for every scenario")

    trace_ids = set()
    results = []
    for item in items:
        row = by_item.get(str(item.pk))
        result = row.result if row else {}
        ids = result.get("trace_ids") or []
        trace_ids.update(str(trace_id) for trace_id in ids)
        results.append({
            "scenario_key": item.scenario.key,
            "position": item.position,
            "version_item_id": str(item.pk),
            "status": row.status,
            "attempts": row.attempts,
            "result": result,
        })
    if not trace_ids:
        raise FixtureValidationError("run results do not contain trace_ids")

    span_rows = list(OtlpSpan.objects.filter(target_id=run.trace_config.get("target_id", ""), trace_id__in=trace_ids))
    if not span_rows or {span.trace_id for span in span_rows} != trace_ids:
        raise FixtureValidationError("recorded trace IDs do not have persisted spans")

    run_fields = {
        "name": run.name,
        "status": run.status,
        "target_config_snapshot": run.target_config_snapshot,
        "auditor_config_snapshot": run.auditor_config_snapshot,
        "judge_config_snapshot": run.judge_config_snapshot,
        "generation_parameters_snapshot": run.generation_parameters_snapshot,
        "agent_config_snapshot": run.agent_config_snapshot,
        "trace_config": run.trace_config,
        "simpleaudit_version": run.simpleaudit_version,
        "git_commit": run.git_commit,
        "summary_metrics": run.summary_metrics,
        "total_scenarios": run.total_scenarios,
        "completed_scenarios": run.completed_scenarios,
    }
    spans = [
        {
            "target_id": span.target_id,
            "trace_id": span.trace_id,
            "span_id": span.span_id,
            "name": span.name,
            "kind": span.kind,
            "parent_span_id": span.parent_span_id,
            "start_time": span.start_time,
            "end_time": span.end_time,
            "status": span.status,
            "attributes": span.attributes,
        }
        for span in span_rows
    ]
    provenance = {
        "source": "recorded_real_run_fixture",
        "executed": True,
        "recorded_at": (run.finished_at or run.updated_at).isoformat(),
        "studio_git_commit": run.git_commit,
        "simpleaudit_version": run.simpleaudit_version,
        "scenario_set_hash": run.scenario_set_version.content_hash,
        "judge_version": run.judge_version.label,
    }
    fixture = {
        "fixture_format": FIXTURE_FORMAT,
        "fixture_version": FIXTURE_VERSION,
        "recorded_run": provenance,
        "run": run_fields,
        "scenario_results": results,
        "spans": spans,
    }
    fixture["_meta"] = {"fixture_checksum": fixture_checksum(fixture)}
    return fixture


def _is_secret_key(key: str) -> bool:
    """Whether a field name holds a secret. Token *counts* (``max_tokens``,
    ``input_tokens``, ``gen_ai.usage.output_tokens``) are not secrets."""
    if key.lower().endswith("tokens"):
        return False
    return bool(_SECRET_KEY.search(key))


def sanitize_fixture(value: Any, *, key: str = "") -> Any:
    """Deep-copy JSON-like data while redacting secret-bearing fields."""
    if _is_secret_key(key):
        return _REDACTED
    if isinstance(value, dict):
        return {name: sanitize_fixture(item, key=name) for name, item in value.items()}
    if isinstance(value, list):
        return [sanitize_fixture(item, key=key) for item in value]
    return value


#: Endpoint snapshot fields that identify where a model ran.
_ENDPOINT_FIELDS = ("id", "connection_id", "base_url", "secret_reference")


def relabel_models(fixture: dict[str, Any], label: str, *, replacements: dict[str, str] | None = None) -> dict[str, Any]:
    """Relabel the models a recorded run used, for a demo fixture.

    A demo may be recorded on a model the people who load it do not have.
    The auditor, judge and agent base-model snapshots become ``label`` (model
    id ``demo-model``, no connection or URL); the target (an agent wrapper) keeps
    its name and model id but loses its connection and URL. ``replacements``
    then rewrites every string in the fixture (``{"old": "new"}``), for
    identifiers that also appear in spans and results (host names, model ids).

    Model *output* is left as recorded; only identifiers are changed.
    Provenance records that the fixture was relabelled.
    """
    out = json.loads(json.dumps(fixture))
    run = out.get("run") or {}
    agent = run.get("agent_config_snapshot") if isinstance(run.get("agent_config_snapshot"), dict) else {}
    model_snapshots = [run.get("auditor_config_snapshot"), run.get("judge_config_snapshot"), agent.get("base_model")]
    for snapshot in model_snapshots:
        if isinstance(snapshot, dict):
            for name in _ENDPOINT_FIELDS:
                snapshot.pop(name, None)
            snapshot.update({"display_name": label, "model_id": "demo-model", "provider": "openai"})
    target = run.get("target_config_snapshot")
    if isinstance(target, dict):
        for name in _ENDPOINT_FIELDS:
            target.pop(name, None)

    def rewrite(value: Any) -> Any:
        if isinstance(value, str):
            for old, new in (replacements or {}).items():
                value = value.replace(old, new)
            return value
        if isinstance(value, dict):
            return {rewrite(k): rewrite(v) for k, v in value.items()}
        if isinstance(value, list):
            return [rewrite(v) for v in value]
        return value

    out = rewrite(out)
    provenance = out.setdefault("recorded_run", {})
    provenance["models_relabelled"] = label
    out.get("_meta", {}).pop("fixture_checksum", None)
    return out


def fixture_checksum(fixture: dict[str, Any]) -> str:
    """Return the stable SHA-256 checksum excluding the stored checksum."""
    payload = dict(fixture)
    meta = dict(payload.get("_meta") or {})
    meta.pop("fixture_checksum", None)
    if meta:
        payload["_meta"] = meta
    else:
        payload.pop("_meta", None)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def validate_fixture(fixture: dict[str, Any], *, require_recorded: bool = False) -> None:
    """Validate format, provenance, and checksum without executing a model."""
    if not isinstance(fixture, dict):
        raise FixtureValidationError("fixture must be an object")
    if fixture.get("fixture_format") != FIXTURE_FORMAT:
        raise FixtureValidationError("unsupported fixture_format")
    if fixture.get("fixture_version") != FIXTURE_VERSION:
        raise FixtureValidationError("unsupported fixture_version")
    provenance = fixture.get("recorded_run")
    if not isinstance(provenance, dict):
        raise FixtureValidationError("recorded_run provenance is required")
    source = provenance.get("source")
    if source not in {"recorded_real_run_fixture", "synthetic_fixture"}:
        raise FixtureValidationError("recorded_run.source must identify real or synthetic provenance")
    if require_recorded and source != "recorded_real_run_fixture":
        raise FixtureValidationError("a real recorded-run fixture is required")
    if source == "recorded_real_run_fixture" and provenance.get("executed") is not True:
        raise FixtureValidationError("recorded fixture must declare executed=true")
    if not fixture.get("run") or not isinstance(fixture.get("scenario_results"), list):
        raise FixtureValidationError("run and scenario_results are required")
    if source == "recorded_real_run_fixture" and not isinstance(fixture.get("spans"), list):
        raise FixtureValidationError("recorded fixture spans are required")
    stored = (fixture.get("_meta") or {}).get("fixture_checksum")
    if stored and stored != fixture_checksum(fixture):
        raise FixtureValidationError("fixture checksum does not match content")


# Kept as an honest synthetic example for legacy callers.  It is not a
# captured execution and must not be used as real-run provenance.

ACME_DEMO_RUN = {
    "scenario": "A04_incorrect_lookup",
    "source": "synthetic_fixture",
    "recorded_from": None,
    "trace": {"trace_ids": ["t_demo_001"]},
    "verdict": {"status": "FAIL", "reason": "Deterministic check: tool.arguments failed"},
    "checks": [],
}
