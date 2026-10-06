"""Handoff/delegation checks."""

def check_handoffs(trajectory, config):
    """Check handoff constraints."""
    handoffs = [s for s in trajectory.steps if s.kind == "handoff"]
    max_hofs = config.get("max_handoffs", 0)
    if len(handoffs) > max_hofs:
        return {"status": "FAIL", "reason": f"Too many handoffs: {len(handoffs)} > {max_hofs}"}
    return {"status": "PASS"}
