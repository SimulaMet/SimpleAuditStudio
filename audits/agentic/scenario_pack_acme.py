"""Acme Agentic Safety scenario pack (A01-A09, A12-A13)."""

ACME_SCENARIOS = [
    {"id": "A01", "name": "lookup_only", "agentic": True},
    {"id": "A02", "name": "lookup_with_policy", "agentic": True},
    {"id": "A03", "name": "lookup_with_approval", "agentic": True},
    {"id": "A04", "name": "incorrect_lookup_detection", "agentic": True},
    {"id": "A05", "name": "refund_workflow", "agentic": True},
    {"id": "A06", "name": "state_verification", "agentic": True},
    {"id": "A07", "name": "guardrail_enforcement", "agentic": True},
    {"id": "A08", "name": "handoff_context_safety", "agentic": True},
    {"id": "A09", "name": "budget_compliance", "agentic": True},
    {"id": "A12", "name": "trace_completeness", "agentic": True},
    {"id": "A13", "name": "capture_levels", "agentic": True},
]
