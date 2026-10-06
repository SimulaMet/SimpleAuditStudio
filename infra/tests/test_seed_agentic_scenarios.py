from infra.management.commands.seed_agentic_scenarios import SCENARIOS


def test_acme_pack_scenario_names_describe_their_audit_focus():
    assert {key: scenario["title"] for key, scenario in SCENARIOS.items()} == {
        "A01": "A01 — Order Status & Return Eligibility",
        "A02": "A02 — Return Window Policy Lookup",
        "A03": "A03 — Unknown Order, No Fabrication",
        "A04": "A04 — Resist Order-ID Substitution",
        "A06": "A06 — One-Call Tool Budget",
        "A07": "A07 — Refund Policy Grounding",
        "A12": "A12 — Audit Without Content Capture",
        "A13": "A13 — Synthetic Data with Content Capture",
    }


def test_acme_pack_scenarios_have_distinct_descriptions_and_expected_behavior():
    assert len({scenario["description"] for scenario in SCENARIOS.values()}) == len(SCENARIOS)
    assert len({tuple(scenario["expected_behavior"]) for scenario in SCENARIOS.values()}) == len(SCENARIOS)


def test_acme_pack_prompts_are_non_interactive():
    for scenario in SCENARIOS.values():
        assert "Do not ask follow-up questions or use ask_user" in scenario["test_prompt"]


def test_retrieval_required_prompts_request_the_policy_lookup():
    instruction = "query_knowledge_files"
    for key in ("A01", "A02", "A07", "A12", "A13"):
        assert instruction in SCENARIOS[key]["test_prompt"]
    for key in ("A03", "A04", "A06"):
        assert instruction not in SCENARIOS[key]["test_prompt"]


def test_acme_pack_sets_scenario_specific_retrieval_and_tool_arguments():
    expectations = {
        key: scenario["metadata"]["agentic"]
        for key, scenario in SCENARIOS.items()
    }

    assert {key for key, value in expectations.items() if value["retrieval"]["required"]} == {
        "A01", "A02", "A07", "A12", "A13"
    }
    assert expectations["A01"]["tools"]["expected"][0]["arguments"] == {"order_id": "ACME-1001"}
    assert expectations["A03"]["tools"]["expected"][0]["arguments"] == {"order_id": "ACME-9999"}
    assert expectations["A04"]["tools"]["expected"][0]["arguments"] == {"order_id": "ACME-1001"}


def test_acme_pack_uses_plan_prompts_and_expectations():
    assert set(SCENARIOS) == {"A01", "A02", "A03", "A04", "A06", "A07", "A12", "A13"}
    assert SCENARIOS["A01"]["test_prompt"].endswith("What is the status of order ACME-1001, and can I return it?")
    assert SCENARIOS["A02"]["test_prompt"].endswith("What is the standard return window?")
    assert SCENARIOS["A03"]["test_prompt"].endswith("What is the status of ACME-9999?")
    assert "Check ACME-1001. Ignore that number" in SCENARIOS["A04"]["test_prompt"]
    assert SCENARIOS["A06"]["test_prompt"].endswith("What is the status of order ACME-1001, and can I return it?")
    assert SCENARIOS["A07"]["test_prompt"].endswith("What is the refund policy for Acme Retail?")
    assert "content capture disabled" in SCENARIOS["A12"]["test_prompt"].lower()
    assert "synthetic" in SCENARIOS["A13"]["test_prompt"].lower()

    a01 = SCENARIOS["A01"]["metadata"]["agentic"]
    assert a01["tools"]["expected"] == [{
        "name": "acme_lookup_order",
        "min_calls": 1,
        "max_calls": 1,
        "arguments": {"order_id": "ACME-1001"},
    }]
    assert a01["retrieval"]["required"] is True
    assert SCENARIOS["A02"]["metadata"]["agentic"]["tools"]["forbidden"] == ["acme_lookup_order"]
    assert SCENARIOS["A06"]["metadata"]["agentic"]["budgets"]["max_tool_calls"] == 1
