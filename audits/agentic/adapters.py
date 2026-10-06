"""Framework-neutral trace adapters (OTEL GenAI, OpenInference, OpenWebUI)."""

def normalize_otel_genai_trace(span: dict) -> dict:
    """Normalize OTEL GenAI span to trajectory step."""
    return {
        "trace_id": span.get("trace_id"),
        "span_id": span.get("span_id"),
        "kind": span.get("attributes", {}).get("gen_ai.operation.name", "unknown"),
        "status": span.get("status", {}).get("status"),
    }

def normalize_openwebui_trace(span: dict) -> dict:
    """Normalize OpenWebUI span to trajectory step."""
    return normalize_otel_genai_trace(span)
