import pytest

from audits.agentic.expectations import validate_agentic_metadata


def test_absent_agentic_metadata_is_a_noop():
    assert validate_agentic_metadata({}) is None


def test_unknown_agentic_key_is_rejected():
    with pytest.raises(ValueError, match="Unknown metadata.agentic keys"):
        validate_agentic_metadata({"agentic": {"schema_version": 1, "unexpected": True}})
