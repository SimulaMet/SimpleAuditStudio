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


def test_token_counts_and_tokenizer_names_are_not_secrets():
    data = {
        "max_tokens": 2048, "input_tokens": 12, "gen_ai.usage.output_tokens": 5,
        "tokenizer_model": "cl100k", "access_token": "a", "x-client-token": "b", "token": "c",
    }
    assert sanitize_fixture(data) == {
        "max_tokens": 2048, "input_tokens": 12, "gen_ai.usage.output_tokens": 5,
        "tokenizer_model": "cl100k", "access_token": "[REDACTED]", "x-client-token": "[REDACTED]",
        "token": "[REDACTED]",
    }


def test_relabel_models_hides_where_a_demo_was_recorded():
    from audits.agentic.demo_fixture import relabel_models

    endpoint = {"id": 7, "connection_id": 3, "display_name": "Qwen (Lab)", "model_id": "default",
                "provider": "openai", "base_url": "https://lab.example/v1", "secret_reference": "LAB_KEY"}
    fixture = {
        "fixture_format": "simpleaudit-agentic-demo", "fixture_version": 1,
        "recorded_run": {"source": "recorded_real_run_fixture", "executed": True},
        "run": {
            "auditor_config_snapshot": dict(endpoint),
            "judge_config_snapshot": {**endpoint, "judge": {"name": "Safety"}},
            "target_config_snapshot": {"display_name": "Agent", "model_id": "studio.agent-1",
                                       "base_url": "http://127.0.0.1:8180/chat/api/v1", "connection_id": 9},
            "agent_config_snapshot": {"name": "Agent", "base_model": dict(endpoint)},
        },
        "scenario_results": [{"result": {"conversation": [{"role": "assistant", "content": "I am Qwen."}]}}],
        "spans": [{"attributes": {"gen_ai.response.model": "Qwen3-27B"}}],
        "_meta": {"fixture_checksum": "old"},
    }
    out = relabel_models(fixture, "Demo model (recorded)", replacements={"Qwen3-27B": "demo-model"})
    for snapshot in (out["run"]["auditor_config_snapshot"], out["run"]["judge_config_snapshot"],
                     out["run"]["agent_config_snapshot"]["base_model"]):
        assert snapshot["display_name"] == "Demo model (recorded)"
        assert snapshot["model_id"] == "demo-model"
        assert not {"base_url", "connection_id", "secret_reference", "id"} & set(snapshot)
    assert out["run"]["judge_config_snapshot"]["judge"] == {"name": "Safety"}
    target = out["run"]["target_config_snapshot"]
    assert target["display_name"] == "Agent" and "base_url" not in target and "connection_id" not in target
    assert out["spans"][0]["attributes"]["gen_ai.response.model"] == "demo-model"
    # Model output stays as recorded; provenance says the labels changed.
    assert out["scenario_results"][0]["result"]["conversation"][0]["content"] == "I am Qwen."
    assert out["recorded_run"]["models_relabelled"] == "Demo model (recorded)"
    assert "fixture_checksum" not in out["_meta"]
    assert fixture["run"]["auditor_config_snapshot"]["base_url"] == "https://lab.example/v1"  # input untouched
