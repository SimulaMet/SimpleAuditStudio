from audits.agentic.schema_v2 import get_schema_v2_template, validate_agentic_metadata


def test_schema_v2_template_is_valid_and_rejects_unknown_fields():
    template = get_schema_v2_template()
    valid, errors = validate_agentic_metadata(template)
    assert valid, errors

    invalid = {**template, "typo": True}
    valid, errors = validate_agentic_metadata(invalid)
    assert not valid
    assert "Unknown field: typo" in errors


