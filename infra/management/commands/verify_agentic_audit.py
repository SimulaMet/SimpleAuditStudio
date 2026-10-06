"""Report locally verifiable Agentic audit acceptance checks.

This command deliberately does not call a target, auditor, judge, or remote
OTLP service. Runtime seams are exercised with an in-process fake harness so
the checks remain safe for local CI and do not claim a paid live run.
"""

from __future__ import annotations

import json
from math import isclose
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.core.management.base import BaseCommand

FIXTURE_PATH = Path(__file__).resolve().parents[2] / "fixtures" / "agentic_trace_verification.json"
REAL_DEMO_FIXTURE_PATH = Path(__file__).resolve().parents[2] / "fixtures" / "acme_agentic_real_run_v1.json"
RESULT_PANEL_PATH = Path(__file__).resolve().parents[3] / "templates" / "partials" / "result_rep_panel.html"
ARCHITECTURE_PATH = Path(__file__).resolve().parents[3] / "docs" / "architecture.md"


def _fixture_payload() -> dict:
    return json.loads(FIXTURE_PATH.read_text())


def _fixture_checks() -> bool:
    """Validate the checked-in trace fixture and normalized relationships."""
    from audits.agentic.trajectory import normalize

    payload = _fixture_payload()
    traces = payload.get("traces", [])
    if payload.get("fixture_format") != "simpleaudit-agentic-trace-verification":
        return False
    if payload.get("fixture_version") != 1 or not traces:
        return False
    trace = traces[0]
    trajectory = normalize(trace["spans"])
    handoffs = trajectory.handoffs()
    tools = trajectory.tools()
    return bool(
        trace.get("trace_id") == "trace-multi-agent-001"
        and handoffs
        and handoffs[0].handoff_to == "refund-specialist"
        and trajectory.children("span-handoff")[0].actor == "refund-specialist"
        and tools[0].parent_span_id == "span-specialist"
        and isclose(tools[0].duration_ms or 0, 20.0)
        and all(step.trace_id == trace["trace_id"] for step in trajectory.steps)
    )


def _schema_check() -> tuple[str, bool, str]:
    from audits.agentic.schema_v2 import (
        get_schema_v2_template,
        validate_agentic_metadata,
    )

    template = get_schema_v2_template()
    valid, errors = validate_agentic_metadata(template)
    if not valid:
        return "schema-v2-validation", False, "; ".join(errors)
    invalid = {**template, "not_a_schema_field": True}
    valid, errors = validate_agentic_metadata(invalid)
    return "schema-v2-validation", not valid and "Unknown field: not_a_schema_field" in errors, "unknown v2 fields were accepted"


def _metadata_roundtrip_check() -> tuple[str, bool, str]:
    """Exercise the serializer contract used by scenario create/import."""
    from scenarios.serializers import (
        ScenarioCreateSerializer,
        ScenarioRevisionSerializer,
    )

    metadata = {"agentic": {"schema_version": 2, "tools": {"forbidden": ["delete"]}}}
    payload = {
        "title": "Round trip", "description": "description",
        "expected_behavior": ["safe"], "test_prompt": "prompt", "metadata": metadata,
    }
    serializer = ScenarioCreateSerializer(data=payload)
    fields = set(ScenarioRevisionSerializer.Meta.fields)
    valid = serializer.is_valid() and serializer.validated_data["metadata"] == metadata
    valid = valid and serializer.validated_data["test_prompt"] == "prompt"
    valid = valid and {"metadata", "test_prompt"}.issubset(fields)
    return "html-metadata-roundtrip", valid, "metadata or test_prompt was not preserved"


def _import_export_check() -> tuple[str, bool, str]:
    from scenarios.serializers import ScenarioCreateSerializer

    source = {
        "key": "agentic-roundtrip", "title": "Agentic", "description": "d",
        "expected_behavior": ["x"], "test_prompt": "p",
        "metadata": {"agentic": {"schema_version": 1}},
    }
    imported = json.loads(json.dumps({"format_version": 2, "scenarios": [source]}))["scenarios"][0]
    serializer = ScenarioCreateSerializer(data=imported)
    valid = serializer.is_valid() and serializer.validated_data["metadata"] == source["metadata"]
    return "import-export-roundtrip", valid, "portable scenario fields did not round-trip"


def _privacy_check() -> tuple[str, bool, str]:
    from audits.agentic.privacy import contains_secret

    if contains_secret(_fixture_payload()):
        return "privacy-no-key-in-source", False, "fixture contains a sensitive key or credential"
    return "privacy-no-key-in-source", True, ""


def _content_capture_check() -> tuple[str, bool, str]:
    from audits.agentic.privacy import sanitize_span

    span = {
        "trace_id": "t", "span_id": "s", "name": "tool",
        "attributes": {
            "gen_ai.tool.call.arguments": '{"order_id":"ACME-1001"}',
            "gen_ai.tool.call.result": "private result",
        },
    }
    structural = sanitize_span(span, "structural")
    attrs = structural.get("attributes", {})
    valid = (
        "gen_ai.tool.call.arguments" not in attrs
        and "gen_ai.tool.call.result" not in attrs
        and structural.get("trace_id") == "t"
        and structural.get("span_id") == "s"
    )
    return "content-off-structural-path", valid, "content fields survived or structural identity was lost"


def _unknown_span_check() -> tuple[str, str, str]:
    from audits.agentic.trajectory import normalize

    trajectory = normalize([{
        "trace_id": "trace-unknown",
        "span_id": "span-unknown",
        "name": "vendor.operation",
        "attributes": {},
    }])
    valid = len(trajectory.steps) == 1 and trajectory.steps[0].kind == "unknown"
    return "unknown-span-preserved", "PASS" if valid else "FAIL", "unrecognized span was dropped or reclassified" if not valid else ""


def _evaluator_check() -> list[tuple[str, bool, str]]:
    from audits.agentic.evaluate import evaluate
    from audits.agentic.schema import AgentTrajectory, TrajectoryStep

    missing = evaluate(AgentTrajectory(), None, {})
    forbidden = evaluate(
        AgentTrajectory(steps=[TrajectoryStep(span_id="tool-1", kind="tool", name="forbidden")]),
        {"agentic": {"tools": {"expected": [], "forbidden": ["forbidden"]}}}, {},
    )
    return [
        ("missing-content-inconclusive", missing.get("status") == "INCONCLUSIVE", repr(missing.get("status"))),
        ("deterministic-tool-check", forbidden.get("status") == "FAIL", repr(forbidden.get("status"))),
    ]


def _trajectory_check() -> tuple[str, bool, str]:
    from audits.agentic.evaluate import evaluate
    from audits.agentic.schema import AgentTrajectory, TrajectoryStep

    trajectory = AgentTrajectory([
        TrajectoryStep(span_id="r", kind="retrieval", name="retrieve"),
        TrajectoryStep(span_id="t", kind="tool", name="lookup"),
    ])
    result = evaluate(trajectory, {"agentic": {
        "trajectory": {
            "required_sequence": [{"kind": "retrieval"}, {"kind": "tool"}],
            "order_mode": "exact",
        },
    }})
    statuses = {item["id"]: item["status"] for item in result["checks"]}
    return "trajectory-sequence-check", statuses.get("trajectory.exact_sequence") == "PASS", repr(statuses)


def _verdict_check() -> tuple[str, bool, str]:
    from audits.agentic.verdict_policy import compute_overall_verdict

    result = compute_overall_verdict({"deterministic_fail": True, "has_inconclusive": True})
    expected = {"status": "FAIL", "reason": "Deterministic check failed"}
    return "agentic-verdict-gating", result == expected, repr(result)


def _fixture_provenance_check() -> tuple[str, str, str]:
    from audits.agentic.demo_fixture import validate_fixture

    if REAL_DEMO_FIXTURE_PATH.is_file():
        try:
            fixture = json.loads(REAL_DEMO_FIXTURE_PATH.read_text())
            validate_fixture(fixture, require_recorded=True)
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            return "real-demo-fixture-checksum", "FAIL", str(exc)
        return "real-demo-fixture-checksum", "PASS", ""

    fixture = {
        "fixture_format": "simpleaudit-agentic-demo", "fixture_version": 1,
        "recorded_run": {"source": "synthetic_fixture", "executed": False},
        "run": {"status": "completed"}, "scenario_results": [],
    }
    try:
        validate_fixture(fixture, require_recorded=True)
    except ValueError:
        return "real-demo-fixture-checksum", "SKIP", "no recorded real-run fixture is checked in"
    return "real-demo-fixture-checksum", "FAIL", "synthetic fixture was accepted as recorded"


def _ui_check() -> tuple[str, bool, str]:
    source = RESULT_PANEL_PATH.read_text()
    valid = "check.evidence_span_ids" in source and "rep.trace" in source
    return "ui-evidence-linking", valid, "result panel does not render check or trace evidence"


def _documentation_check() -> tuple[str, bool, str]:
    source = ARCHITECTURE_PATH.read_text().lower()
    valid = "agentic" in source and "trace" in source
    return "documentation", valid, "architecture documentation omits agentic trace behavior"


def _trace_runtime_checks() -> list[tuple[str, str, str]]:
    """Exercise correlation and resolver ordering without paid model calls.

    This is intentionally a small production-seam harness, rather than a
    claim that a real target exported OTLP.  A fake auditor emits a trace id,
    invokes the same ``evidence_resolver`` hook the engine uses, and then
    performs its fake judge step.  The real Studio ``run_scenario`` wiring and
    evidence selection therefore remain under test.
    """
    from infra.engine import run_scenario

    trace_id = "a" * 32
    events: list[str] = []

    class LocalTraceProvider:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return None

        def fetch(self, requested_trace_id):
            if requested_trace_id != trace_id:
                return []
            return [{
                "trace_id": trace_id,
                "span_id": "b" * 16,
                "name": "model.call",
                "kind": "LLM",
                "start_time": 1.0,
                "end_time": 1.1,
            }]

    class LocalAuditor:
        async def run_async(self, _scenarios, **kwargs):
            events.append("target")
            correlation = kwargs["trace_correlation"]
            correlation.record("turn-1", trace_id)
            execution = SimpleNamespace(trace_correlation=correlation)
            selected = kwargs["evidence_resolver"](execution)
            events.append("judge")
            return [SimpleNamespace(to_dict=lambda: {
                "severity": "pass",
                "judgment": {"evidence_spans": selected or []},
            })]

    try:
        with (
            patch("infra.engine.build_model_auditor", return_value=(LocalAuditor(), "English")),
            patch("infra.tracing.build_trace_provider", return_value=LocalTraceProvider()),
        ):
            payload = run_scenario(
                name="local-harness",
                description="local harness",
                expected_behavior=None,
                test_prompt="hello",
                target={"model_id": "target"},
                auditor={"model_id": "auditor"},
                judge={"model_id": "judge"},
                trace_config={"mode": "tempo", "base_url": "http://local-harness"},
            )
    except Exception as exc:  # noqa: BLE001 - acceptance check must report failures
        detail = f"local trace harness crashed: {exc}"
        return [("otlp-correlation", "FAIL", detail), ("trace-before-judge", "FAIL", detail)]

    evidence = (payload.get("judgment") or {}).get("evidence_spans") or []
    correlation_ok = payload.get("trace_ids") == [trace_id] and evidence
    ordering_ok = events == ["target", "judge"] and evidence
    return [
        ("otlp-correlation", "PASS" if correlation_ok else "FAIL",
         "local harness did not preserve the emitted trace id" if not correlation_ok else ""),
        ("trace-before-judge", "PASS" if ordering_ok else "FAIL",
         "evidence was not available before the fake judge step" if not ordering_ok else ""),
    ]


class Command(BaseCommand):
    help = "Verify locally executable Agentic audit wiring without model or API calls."

    def handle(self, *args, **options):
        from model_registry.models import Agent, ModelConnection

        checks: list[tuple[str, str, str]] = []
        for name, passed, detail in [_schema_check(), _metadata_roundtrip_check(), _import_export_check(), _privacy_check(), _content_capture_check(), _trajectory_check(), _verdict_check(), _ui_check(), _documentation_check()]:
            checks.append((name, "PASS" if passed else "FAIL", "" if passed else detail))
        for name, passed, detail in _evaluator_check():
            checks.append((name, "PASS" if passed else "FAIL", "" if passed else detail))
        name, status, detail = _fixture_provenance_check()
        checks.append((name, status, detail))
        checks.extend([
            ("trace-fixture", "PASS" if FIXTURE_PATH.is_file() and _fixture_checks() else "FAIL", ""),
            _unknown_span_check(),
            ("acme-pack-schema", "PASS" if self._acme_pack_is_valid() else "FAIL", ""),
            (
                "connection",
                "PASS" if ModelConnection.objects.filter(
                    name__in=("Open WebUI Agents", "SimulaChat"), enabled=True
                ).exists() else "FAIL",
                "",
            ),
            ("agent", "PASS" if Agent.objects.filter(enabled=True, external_id__startswith="studio.agent-").exists() else "FAIL", ""),
        ])
        checks.extend(_trace_runtime_checks())
        failed = 0
        for name, status, detail in checks:
            if status == "FAIL":
                failed += 1
            suffix = f"\t{detail}" if detail else ""
            self.stdout.write(f"{status}\t{name}{suffix}")
        if failed:
            self.stderr.write(self.style.ERROR(f"{failed} acceptance check(s) failed."))
            raise SystemExit(1)
        passed = sum(status == "PASS" for _, status, _ in checks)
        skipped = sum(status == "SKIP" for _, status, _ in checks)
        self.stdout.write(self.style.SUCCESS(f"{passed} acceptance checks passed; {skipped} skipped (runtime-dependent)."))

    @staticmethod
    def _acme_pack_is_valid() -> bool:
        from audits.agentic.schema_v2 import validate_agentic_metadata
        from infra.management.commands.seed_agentic_scenarios import SCENARIOS

        return bool(SCENARIOS) and all(
            validate_agentic_metadata(spec.get("metadata", {}).get("agentic", {}))[0]
            for spec in SCENARIOS.values()
        )
