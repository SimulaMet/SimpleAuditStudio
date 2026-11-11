"""Which role failed a scenario, and why (structured, best effort).

A failed scenario stores a ``failure`` object next to the raw error text::

    {"role": "target|auditor|judge|system",   # required
     "kind": "rate_limit|unavailable|auth|timeout|bad_response|config|unknown",
     "message": "raw text", "http_status": 429, "endpoint": "...",
     "model": "...", "retryable": true, "attempt": 2}

``role`` is the only required field; the rest is derived from the error text
and the frozen endpoint snapshot, and left out when unknown. Nothing here
assumes a target type or provider. Results stored without ``failure`` (older
runs) have no structure and render as before.
"""

from __future__ import annotations

import re
from typing import Any

ROLES = ("target", "auditor", "judge", "system")
ROLE_LABELS = {"target": "Target", "auditor": "Auditor", "judge": "Judge", "system": "System"}
KINDS = ("rate_limit", "unavailable", "auth", "timeout", "bad_response", "config", "unknown")
KIND_LABELS = {
    "rate_limit": "rate limited",
    "unavailable": "unavailable",
    "auth": "auth rejected",
    "timeout": "timeout",
    "bad_response": "bad response",
    "config": "misconfigured",
    "unknown": "",
}
_RETRYABLE = {"rate_limit", "unavailable", "timeout", "bad_response"}

# Exception class names that imply a status when the message carries no number.
_NAME_STATUS = (
    ("RateLimit", 429), ("AuthenticationError", 401), ("PermissionDenied", 403),
    ("ServiceUnavailable", 503), ("InternalServerError", 500), ("BadGateway", 502),
)
_STATUS_RE = re.compile(
    r"(?:status|http|code|error)\W{0,4}([45]\d\d)\b"
    r"|\b([45]\d\d)\s+(?:too many|internal|bad gateway|service|gateway|unauthorized|forbidden|not found)",
    re.IGNORECASE,
)


def _http_status(text: str) -> int | None:
    match = _STATUS_RE.search(text)
    if match:
        return int(match.group(1) or match.group(2))
    for name, status in _NAME_STATUS:
        if name in text:
            return status
    return None


def classify_kind(text: str, http_status: int | None = None) -> str:
    """Provider-agnostic cause from an HTTP status or the error wording."""
    low = (text or "").lower()
    if http_status == 429 or "ratelimit" in low or "rate limit" in low or "rate_limit" in low or "too many requests" in low:
        return "rate_limit"
    if http_status in (401, 403) or any(w in low for w in ("authentication", "unauthorized", "forbidden", "invalid api key", "permission denied")):
        return "auth"
    if http_status == 408 or http_status == 504 or "timeout" in low or "timed out" in low:
        return "timeout"
    if "is not set" in low and "environment variable" in low:
        return "config"
    if http_status is not None and http_status >= 500 or any(
        w in low for w in ("unavailable", "connection", "overloaded", "bad gateway", "internal server error", "temporarily", "unreachable", "name resolution")
    ):
        return "unavailable"
    if any(w in low for w in ("could not parse", "no choices", "empty response", "successful (http 200)", "returned an error", "no message", "parse")):
        return "bad_response"
    if http_status is not None and 400 <= http_status < 500:
        return "config"
    return "unknown"


def build_failure(
    role: str,
    message: str,
    *,
    endpoint: str | None = None,
    model: str | None = None,
    attempt: int | None = None,
    kind: str | None = None,
    http_status: int | None = None,
) -> dict[str, Any]:
    """A failure object; ``role`` is required, everything else only when known."""
    role = role if role in ROLES else "system"
    message = str(message or "")
    status = http_status if http_status is not None else _http_status(message)
    kind = kind if kind in KINDS else classify_kind(message, status)
    failure: dict[str, Any] = {"role": role, "kind": kind, "message": message}
    if status is not None:
        failure["http_status"] = status
    if endpoint:
        failure["endpoint"] = endpoint
    if model:
        failure["model"] = model
    if kind != "unknown":
        failure["retryable"] = kind in _RETRYABLE
    if attempt is not None:
        failure["attempt"] = attempt
    return failure


def endpoint_of(snapshot: dict | None) -> dict[str, str]:
    """Endpoint/model of a frozen role snapshot, only the parts it records."""
    snap = snapshot if isinstance(snapshot, dict) else {}
    out = {}
    url = snap.get("base_url") or ""
    # Never persist credentials that ride in a URL.
    if url:
        out["endpoint"] = re.sub(r"//[^/@]*@", "//", str(url))
    model = snap.get("model_id") or ""
    if model:
        out["model"] = str(model)
    return out


class PhaseTracker:
    """Last phase that completed, per repetition, from the engine's ``on_turn``.

    The engine fires ``on_turn`` after a phase returns and fires nothing for a
    phase that fails, so the failed phase is the one after the last that
    completed: ``auditor`` -> ``target`` -> next turn's ``auditor``.
    """

    def __init__(self) -> None:
        self._last: dict[int, tuple[int, int, str]] = {}

    def note(self, rep: int, turn: int, max_turns: int, role: str) -> None:
        self._last[rep] = (turn, max_turns, role)

    def failed_role(self, rep: int, error_phase: str | None, *, probe_is_fixed: bool = False) -> str:
        """Role that failed in repetition ``rep``; ``system`` when not attributable."""
        if error_phase == "judge":
            return "judge"
        last = self._last.get(rep)
        if last is None:
            # Nothing completed: the first call. A fixed test prompt skips the auditor.
            return "target" if probe_is_fixed else "auditor"
        turn, max_turns, role = last
        if role == "auditor":
            return "target"
        if role == "target":
            return "auditor" if turn < max_turns - 1 else "system"
        return "system"


def attach_failures(
    payload: dict,
    tracker: PhaseTracker,
    snapshots: dict[str, dict | None],
    *,
    attempt: int | None = None,
    probe_is_fixed: bool = False,
    reps: bool = False,
) -> dict | None:
    """Stamp ``failure`` onto an engine result (and each ERROR rep); return the first.

    Mutates ``payload``. ``snapshots`` maps role -> frozen endpoint snapshot.
    A semantic judge failure recorded by the worker (``failure`` already set)
    is left alone.
    """
    first = None

    def one(item: dict, rep: int) -> dict | None:
        if str(item.get("severity", "")).upper() != "ERROR":
            return None
        if isinstance(item.get("failure"), dict):
            return item["failure"]
        judgment = item.get("judgment") if isinstance(item.get("judgment"), dict) else {}
        message = str(judgment.get("error") or item.get("error") or item.get("summary") or "")
        role = tracker.failed_role(rep, judgment.get("error_phase"), probe_is_fixed=probe_is_fixed)
        failure = build_failure(role, message, attempt=attempt, **endpoint_of(snapshots.get(role)))
        item["failure"] = failure
        return failure

    rep_items = payload.get("reps")
    if isinstance(rep_items, list):
        for index, rep in enumerate(rep_items):
            if isinstance(rep, dict):
                got = one(rep, index)
                first = first or got
        if first is not None:
            payload["failure"] = first
    else:
        first = one(payload, 0)
    return first


def result_failure(payload: Any) -> dict | None:
    """The stored structured failure of a result, or None (old results)."""
    payload = payload if isinstance(payload, dict) else {}
    failure = payload.get("failure")
    if isinstance(failure, dict) and failure.get("role") in ROLES:
        return failure
    for rep in payload.get("reps") or []:
        failure = rep.get("failure") if isinstance(rep, dict) else None
        if isinstance(failure, dict) and failure.get("role") in ROLES:
            return failure
    return None


def failure_label(failure: dict | None) -> str:
    """``"Target · rate limited"`` style badge text."""
    if not failure:
        return ""
    label = ROLE_LABELS.get(failure.get("role"), "System")
    cause = KIND_LABELS.get(failure.get("kind") or "", "")
    return f"{label} · {cause}" if cause else label


def failure_where(failure: dict | None) -> str:
    """Model if known, else the endpoint; never an assumed model."""
    if not failure:
        return ""
    return failure.get("model") or failure.get("endpoint") or ""


def count_by_role(results: list[dict] | Any) -> dict[str, int]:
    """Failures per role over stored result payloads that failed (role-less ones skipped)."""
    counts: dict[str, int] = {}
    for payload in results:
        failure = result_failure(payload)
        if failure:
            counts[failure["role"]] = counts.get(failure["role"], 0) + 1
    return counts


def summary_text(counts: dict[str, int], total_failed: int) -> str:
    """``"47 failed: 44 target, 3 judge"``; just the total when roles are unknown."""
    parts = [f"{counts[r]} {r}" for r in ROLES if counts.get(r)]
    return f"{total_failed} failed: {', '.join(parts)}" if parts else f"{total_failed} failed"


_SECRET_ROLE_RE = re.compile(r"Secret for (target|auditor|judge) endpoint", re.IGNORECASE)


def engine_error_failure(message: str, snapshots: dict[str, dict | None], *, attempt: int | None = None) -> dict:
    """Failure for an ``EngineError`` raised before/around the conversation.

    These are setup or crash errors that belong to no single call, so ``system``,
    unless the message names the endpoint whose configuration is broken.
    """
    match = _SECRET_ROLE_RE.search(message or "")
    role = match.group(1).lower() if match else "system"
    return build_failure(role, message, attempt=attempt, **endpoint_of(snapshots.get(role)))
