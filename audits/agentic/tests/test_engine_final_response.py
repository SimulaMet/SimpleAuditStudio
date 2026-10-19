"""The engine surfaces the last assistant turn as judgment.final_response.

The agentic semantic judge's evidence packet needs the response it is judging;
without it the packet carries an empty final_response and the judge can only
reason from spans.
"""
from infra.engine import _attach_final_response


def test_attaches_last_assistant_turn():
    payload = {
        "conversation": [
            {"role": "user", "content": "Check ACME-9999."},
            {"role": "assistant", "content": ""},
            {"role": "assistant", "content": "No record found for ACME-9999."},
        ],
        "judgment": {"severity": "pass"},
    }
    _attach_final_response(payload)
    assert payload["judgment"]["final_response"] == "No record found for ACME-9999."


def test_skips_empty_trailing_assistant_turns():
    payload = {
        "conversation": [
            {"role": "assistant", "content": "Here are the details."},
            {"role": "assistant", "content": "   "},
        ],
    }
    _attach_final_response(payload)
    assert payload["judgment"]["final_response"] == "Here are the details."


def test_no_conversation_leaves_payload_untouched():
    payload = {"judgment": {"severity": "pass"}}
    _attach_final_response(payload)
    assert "final_response" not in payload["judgment"]


def test_existing_final_response_is_not_overwritten():
    payload = {
        "conversation": [{"role": "assistant", "content": "new"}],
        "judgment": {"final_response": "original"},
    }
    _attach_final_response(payload)
    assert payload["judgment"]["final_response"] == "original"
