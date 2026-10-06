from audits.agentic.privacy import apply_content_capture_level


def test_structural_capture_drops_content_and_redacts_secrets():
    trace = {
        "span_id": "s1", "kind": "tool", "status": "OK",
        "arguments": {"order": "A-1", "api_key": "secret"},
        "result": {"body": "private"},
    }
    captured = apply_content_capture_level([trace], "structural")[0]
    assert captured["span_id"] == "s1"
    assert captured["kind"] == "tool"
    assert captured["status"] == "OK"
    assert captured["capture_level"] == "structural"


def test_inputs_capture_keeps_inputs_but_not_outputs():
    trace = {"span_id": "s1", "arguments": {"token": "secret", "q": "refund"}, "result": "private"}
    captured = apply_content_capture_level([trace], "inputs")[0]
    assert captured["arguments"] == {"token": "[REDACTED]", "q": "refund"}
    assert "result" not in captured


def test_unknown_capture_level_fails_closed():
    captured = apply_content_capture_level([{"span_id": "s1", "result": "private"}], "typo")
    assert captured == [{"span_id": "s1", "capture_level": "structural"}]
