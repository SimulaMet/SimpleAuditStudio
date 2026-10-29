import pytest

from audits.agentic.demo_fixture import (
    FixtureValidationError,
    fixture_checksum,
    sanitize_fixture,
    validate_fixture,
)


def _fixture(source="synthetic_fixture"):
    fixture = {
        "fixture_format": "simpleaudit-agentic-demo",
        "fixture_version": 1,
        "recorded_run": {"source": source, "executed": source == "recorded_real_run_fixture"},
        "run": {"status": "completed"},
        "scenario_results": [],
    }
    fixture["_meta"] = {"fixture_checksum": fixture_checksum(fixture)}
    return fixture


def test_sanitizer_redacts_secret_fields_recursively():
    clean = sanitize_fixture({"headers": {"Authorization": "Bearer secret"}, "order": "ACME-1001"})
    assert clean["headers"]["Authorization"] == "[REDACTED]"
    assert clean["order"] == "ACME-1001"


def test_synthetic_fixture_is_not_accepted_as_recorded():
    with pytest.raises(FixtureValidationError, match="real recorded-run"):
        validate_fixture(_fixture(), require_recorded=True)


def test_recorded_fixture_requires_execution_provenance():
    fixture = _fixture("recorded_real_run_fixture")
    fixture["recorded_run"]["executed"] = False
    fixture["_meta"]["fixture_checksum"] = fixture_checksum(fixture)
    with pytest.raises(FixtureValidationError, match="executed=true"):
        validate_fixture(fixture, require_recorded=True)
