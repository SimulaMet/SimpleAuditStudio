"""Base check infrastructure and severity semantics."""
from dataclasses import dataclass, field
from typing import Any


@dataclass
class CheckResult:
    """Standard result for a deterministic check."""
    id: str
    category: str
    status: str  # PASS, FAIL, INCONCLUSIVE, ERROR
    severity: str = ""
    summary: str = ""
    expected: Any = None
    observed: Any = None
    evidence_span_ids: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)


def result(check_id: str, category: str, status: str, summary: str, **kwargs) -> CheckResult:
    """Create a check result with standard fields."""
    return CheckResult(check_id, category, status, summary=summary, **kwargs)


def apply_severity(
    check_result: CheckResult,
    severity_overrides: dict[str, str] | None = None,
    default_severity: str = "medium",
) -> CheckResult:
    """Apply severity to a check result from overrides or defaults.

    Severity mapping:
    - FAIL: use override or default (typically high/critical)
    - INCONCLUSIVE: medium
    - ERROR: high
    - PASS: low
    """
    if check_result.severity:
        return check_result  # Already has explicit severity

    severity_overrides = severity_overrides or {}
    status = check_result.status.upper()

    if status == "PASS":
        check_result.severity = "low"
    elif status == "ERROR":
        check_result.severity = "high"
    elif status == "INCONCLUSIVE":
        check_result.severity = "medium"
    elif status == "FAIL":
        check_result.severity = severity_overrides.get(check_result.id, default_severity)
    else:
        check_result.severity = default_severity

    return check_result
