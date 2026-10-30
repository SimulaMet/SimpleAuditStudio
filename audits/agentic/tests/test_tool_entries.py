"""Builder text for forbidden tools: names and name(arg=value) entries."""
import pytest

from audits.agentic.tool_entries import format_tool_entries, parse_tool_entries


def test_plain_names_stay_strings():
    assert parse_tool_entries("send_email, acme_create_refund_request") == [
        "send_email",
        "acme_create_refund_request",
    ]


def test_argument_entries_become_name_and_arguments():
    assert parse_tool_entries('lookup(id=42, mode="full"), send_email') == [
        {"name": "lookup", "arguments": {"id": 42, "mode": "full"}},
        "send_email",
    ]


def test_plain_string_values_are_kept_as_strings():
    assert parse_tool_entries("lookup(id=SECRET-42)") == [{"name": "lookup", "arguments": {"id": "SECRET-42"}}]


def test_format_round_trips_the_builder_text():
    text = 'lookup(id=42, mode="full"), send_email'
    assert format_tool_entries(parse_tool_entries(text)) == text


def test_empty_text_gives_no_entries():
    assert parse_tool_entries("") == []
    assert format_tool_entries(None) == ""


@pytest.mark.parametrize("bad", ["lookup(id)", "lookup(=42)", "bad name", "lookup(id=1"])
def test_malformed_entries_are_rejected(bad):
    with pytest.raises(ValueError):
        parse_tool_entries(bad)


def test_form_value_is_saved_as_forbidden_argument_entry():
    from infra.ui import _merge_structured_agentic_fields

    post = {"agentic_tools_forbidden": 'lookup(id=SECRET-42), send_email'}
    metadata = _merge_structured_agentic_fields({}, post)
    assert metadata["agentic"]["tools"]["forbidden"] == [
        {"name": "lookup", "arguments": {"id": "SECRET-42"}},
        "send_email",
    ]
