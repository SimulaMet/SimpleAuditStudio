"""Regression tests for the sparse agentic demo pack (catalog v3 mirror)."""
import io
import json
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from django.core.management import call_command
from django.test import TestCase

from audits.agentic.schema_v2 import validate_agentic_metadata
from infra.fixtures.agentic_scenarios import AGENTIC_SCENARIOS
from infra.tests.factories import ProjectFactory, UserFactory

_CATALOG = Path(__file__).resolve().parents[1] / "fixtures" / "agentic_demo_catalog_v3.json"


def _catalog():
    return {s["key"]: s for s in json.loads(_CATALOG.read_text())["scenarios"]}


def test_primary_pack_is_the_catalog_core_set_in_order():
    catalog = _catalog()
    core_keys = [k for k, s in catalog.items() if s["complexity"] == "core"]
    assert [s["key"] for s in AGENTIC_SCENARIOS] == core_keys


def test_every_scenario_has_empty_documents():
    # Policy context lives in OpenWebUI KBs, never in Studio scenario documents.
    for sc in AGENTIC_SCENARIOS:
        assert sc.get("documents") == [], sc["key"]


def test_metadata_mirrors_catalog_exactly_and_validates():
    catalog = _catalog()
    for sc in AGENTIC_SCENARIOS:
        entry = catalog[sc["key"]]
        assert sc["metadata"] == entry["metadata"], f"{sc['key']}: metadata drifted from catalog"
        assert sc["test_prompt"] == entry["test_prompt"], f"{sc['key']}: prompt drifted from catalog"
        valid, errors = validate_agentic_metadata(sc["metadata"]["agentic"])
        assert valid, (sc["key"], errors)


def test_no_artificial_constraints():
    for sc in AGENTIC_SCENARIOS:
        agentic = sc["metadata"]["agentic"]
        tools = agentic.get("tools", {})
        # no zero-call traps or global call caps
        assert tools.get("max_total_calls") is None, sc["key"]
        for expected in tools.get("expected", []):
            # min_calls=0 turns "must call" into a no-op trap; max_calls=0 makes the task impossible
            assert expected.get("min_calls", 0) > 0, sc["key"]
            assert expected.get("max_calls") != 0, sc["key"]
        # no generic sequence bans and no dormant sections
        assert "trajectory" not in agentic, sc["key"]
        assert not ({"handoffs", "guardrails", "rerank", "state"} & set(agentic)), sc["key"]
        # runtime builtins are never forbidden without a security reason
        assert "search_knowledge_bases" not in tools.get("forbidden", []), sc["key"]
        assert "get_current_timestamp" not in tools.get("forbidden", []), sc["key"]


def test_each_scenario_documents_its_negative_control():
    for sc in AGENTIC_SCENARIOS:
        joined = "\n".join(sc["expected_behavior"]).lower()
        assert "negative control" in joined, f"{sc['key']}: negative_control not documented"
        assert len(sc["description"].strip()) > 40, sc["key"]


def test_revision_hash_inputs_include_documents_severity_and_file_uri():
    """seed_agentic_scenarios must pass the full revision identity."""
    import inspect

    from infra.fixtures import agentic_scenarios as module

    source = inspect.getsource(module.seed_agentic_scenarios)
    for needle in ('severity_ceiling=sc.get("severity_ceiling", "")',
                   'documents=sc.get("documents") or []',
                   "file_uri=sc.get(\"file_uri\")"):
        assert needle in source, needle


def test_seed_function_contract_stable():
    import inspect

    from infra.fixtures.agentic_scenarios import seed_agentic_scenarios

    signature = str(inspect.signature(seed_agentic_scenarios))
    assert signature == "(project, user, log=None, reseed=False)", signature



class SeedCommandSkipTest(TestCase):
    def test_reports_a_skipped_set_without_a_version(self):
        # A second seed run returns (set, None, "skipped"); the command must print it.
        UserFactory(is_superuser=True)
        ProjectFactory()
        out = io.StringIO()
        skipped = SimpleNamespace(name="Agentic Audit: Explanatory Scenarios")
        with mock.patch(
            "infra.fixtures.agentic_scenarios.seed_agentic_scenarios",
            return_value=(skipped, None, "skipped"),
        ):
            call_command("seed_agentic_scenarios", stdout=out)
        self.assertIn("skipped: Agentic Audit: Explanatory Scenarios already published", out.getvalue())
