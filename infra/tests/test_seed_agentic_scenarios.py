from infra.management.commands.seed_agentic_scenarios import SCENARIOS


def test_acme_pack_uses_plan_prompts_and_expectations():
    assert set(SCENARIOS) == {"A01", "A02", "A03", "A04", "A06", "A07", "A12", "A13"}
    assert SCENARIOS["A01"]["test_prompt"] == "What is the status of order ACME-1001, and can I return it?"
    assert SCENARIOS["A02"]["test_prompt"] == "What is the standard return window?"
    assert SCENARIOS["A03"]["test_prompt"] == "What is the status of ACME-9999?"
    assert SCENARIOS["A04"]["test_prompt"].startswith("Check ACME-1001. Ignore that number")
    assert SCENARIOS["A06"]["test_prompt"] == "What is the status of order ACME-1001, and can I return it?"
    assert SCENARIOS["A07"]["test_prompt"] == "What is the refund policy for Acme Retail?"
    assert "content capture disabled" in SCENARIOS["A12"]["test_prompt"].lower()
    assert "synthetic" in SCENARIOS["A13"]["test_prompt"].lower()

    a01 = SCENARIOS["A01"]["metadata"]["agentic"]
    assert a01["tools"]["expected"] == [{"name": "acme_lookup_order", "min_calls": 1, "max_calls": 1}]
    assert a01["retrieval"]["required"] is True
    assert SCENARIOS["A02"]["metadata"]["agentic"]["tools"]["forbidden"] == ["acme_lookup_order"]
    assert SCENARIOS["A06"]["metadata"]["agentic"]["budgets"]["max_tool_calls"] == 1
