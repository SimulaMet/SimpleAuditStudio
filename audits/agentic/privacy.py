"""Privacy / content-capture level enforcement."""

def apply_content_capture_level(traces: list, level: str) -> list:
    """Filter traces by content capture level."""
    if level == "structural":
        return [{k: v for k, v in t.items() if k in ["span_id", "kind", "status"]} for t in traces]
    if level == "inputs":
        return [{k: v for k, v in t.items() if k not in ["result"]} for t in traces]
    return traces
