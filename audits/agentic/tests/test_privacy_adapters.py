import json

from audits.agentic.adapters.openinference import normalize as normalize_openinference
from audits.agentic.adapters.openwebui import normalize as normalize_openwebui
from audits.agentic.adapters.otel_genai import normalize as normalize_otel
from audits.agentic.privacy import apply_content_capture_level, serialize_for_judge

SECRET = "sk-test-123456789012345"


def _span():
    return {
        "trace_id": "trace-1",
        "span_id": "span-1",
        "name": "execute_tool",
        "kind": "tool",
        "status": "OK",
        "attributes": {
            "gen_ai.tool.name": "lookup",
            "gen_ai.tool.call.arguments": json.dumps({"order": "123"}),
            "gen_ai.tool.call.result": {"email": "user@example.test"},
            "authorization": f"Bearer {SECRET}",
        },
    }


def test_capture_levels_are_additive_and_redact_before_persistence_or_judge():
    structural = apply_content_capture_level([_span()], "structural")[0]
    inputs = apply_content_capture_level([_span()], "inputs")[0]
    full = serialize_for_judge([_span()], "full")[0]

    assert "gen_ai.tool.call.arguments" not in structural["attributes"]
    assert "gen_ai.tool.call.arguments" in inputs["attributes"]
    assert "gen_ai.tool.call.result" in full["attributes"]
    encoded = json.dumps([structural, inputs, full])
    assert SECRET not in encoded
    assert "authorization" in encoded  # key remains useful for auditability
    assert "[REDACTED]" in encoded


def test_provider_adapters_return_common_trajectory_and_preserve_missing_content():
    otel = normalize_otel([_span()])
    openinference = normalize_openinference([{
        "span_id": "i1", "name": "tool", "attributes": {
            "openinference.span.kind": "TOOL",
            "tool.name": "lookup",
            "tool.parameters": {"order": "123"},
        },
    }])
    openwebui = normalize_openwebui([{
        "span_id": "w1", "name": "owui tool",
        "attributes": {"openwebui.span.kind": "TOOL", "openwebui.tool.name": "search"},
    }])

    assert otel.tools()[0].name == "lookup"
    assert otel.tools()[0].result == {"email": "user@example.test"}
    assert openinference.tools()[0].kind == "tool"
    assert openinference.tools()[0].arguments == {"order": "123"}
    assert openwebui.tools()[0].name == "search"
    assert openwebui.tools()[0].arguments is None
