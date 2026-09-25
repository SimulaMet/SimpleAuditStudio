"""System health probes for the admin Health panel.

Each probe returns a small structured dict and is wrapped so that ONE failing
component never breaks the whole payload: a probe that raises or times out
reports ``status="down"`` (or ``"unknown"``) with a detail string, while every
other component still reports normally. This is what makes the panel useful as a
bottleneck finder — you can see exactly which piece is red.

Design constraints:
- No new dependencies. Host metrics come from ``/proc`` (Linux containers),
  ``os.getloadavg()``, and ``shutil.disk_usage``. Per-process RSS is read from
  ``/proc/<pid>/statm``. On non-Linux dev machines the host probes degrade to
  ``"unknown"`` rather than crashing.
- Probes are pure functions of the current environment; they take no arguments
  and return plain JSON-serializable dicts.
- The web process only ever probes things it can reach itself (DB, Hatchet HTTP,
  MinIO, its own process). It cannot directly observe the worker process's
  memory; worker liveness is inferred from the Hatchet worker registry / recent
  activity, and worker resource figures are reported as ``None`` when unknown.
"""
from __future__ import annotations

import os
import shutil
import time
from typing import Any

# A probe slower than this is "degraded" (amber) even if it ultimately succeeds.
DEGRADED_LATENCY_MS = 500


def _now_ms() -> float:
    return time.monotonic() * 1000.0


def _probe(fn):
    """Run ``fn`` and normalize its result to a safe component dict.

    ``fn`` should return a dict with at least ``status`` ("up"/"down"/"unknown")
    and may include ``latency_ms`` and arbitrary signal keys. If ``fn`` raises,
    we return a down component with the exception summary instead of propagating.
    """
    start = _now_ms()
    try:
        result = fn()
    except Exception as exc:  # noqa: BLE001 - a probe must never break the panel
        result = {"status": "down", "detail": f"{type(exc).__name__}: {exc}"}
    latency = round(_now_ms() - start, 1)
    result.setdefault("latency_ms", latency)
    # An otherwise-healthy probe that was slow is worth flagging as degraded.
    if result.get("status") == "up" and latency > DEGRADED_LATENCY_MS:
        result["status"] = "degraded"
        result.setdefault("detail", f"slow response ({latency} ms)")
    return result


# ─── Component probes ────────────────────────────────────────────────────────

def _postgres_probe() -> dict[str, Any]:
    from django.db import connection

    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
        cursor.fetchone()
    signals: dict[str, Any] = {}
    # Database size (Postgres-specific); best-effort, ignore on failure.
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_database_size(current_database())")
            row = cursor.fetchone()
            if row:
                signals["db_size_bytes"] = int(row[0])
    except Exception:  # noqa: BLE001,S110 - non-Postgres or permission issue; size is optional
        pass
    return {"status": "up", **signals}


def _hatchet_probe() -> dict[str, Any]:
    from django.conf import settings

    # In minimal config, the embedded Hatchet API requires an auth token.
    # Instead of hitting the HTTP endpoint, just check the client is alive.
    if getattr(settings, "MINIMAL_CONFIG", False):
        from infra.minimal_config import get_embedded_client

        client = get_embedded_client()
        if client is not None:
            return {"status": "up", "detail": "embedded"}
        return {"status": "down", "detail": "embedded client not started"}

    import requests

    base = getattr(settings, "HATCHET_SERVER_URL", "").rstrip("/")
    if not base:
        return {"status": "unknown", "detail": "HATCHET_SERVER_URL not configured"}
    resp = requests.get(f"{base}/healthz", timeout=3)
    resp.raise_for_status()
    return {"status": "up"}


def _worker_probe() -> dict[str, Any]:
    """Infer worker liveness from recent durable activity.

    We do not hold a live gRPC handle to the worker here; instead we treat
    "a scenario was executed recently" as evidence the worker is alive. This is
    a proxy, but it is durable (reads the DB) and needs no extra connection.
    """
    from django.utils import timezone

    from audits.models import AuditRun

    active_statuses = [
        AuditRun.Status.PREPARING,
        AuditRun.Status.TARGET_EXECUTION,
        AuditRun.Status.AUDITING,
        AuditRun.Status.JUDGING,
        AuditRun.Status.AGGREGATION,
        AuditRun.Status.REPORT_GENERATION,
    ]
    active_count = AuditRun.objects.filter(status__in=active_statuses).count()
    if active_count:
        return {"status": "up", "active_runs": active_count}

    # No active runs: look for a run that started within the last 90s.
    recent = AuditRun.objects.filter(
        started_at__gte=timezone.now() - timezone.timedelta(seconds=90)
    ).count()
    if recent:
        return {"status": "up", "recently_active": True}

    # Idle is not "down" — a healthy system with no work looks idle. Report up
    # but make clear there is no in-flight work, so an operator isn't alarmed.
    return {"status": "up", "idle": True}


def _minio_probe() -> dict[str, Any]:
    from django.conf import settings

    endpoint = getattr(settings, "MINIO_ENDPOINT", "")
    access_key = getattr(settings, "MINIO_ACCESS_KEY", "")
    secret_key = getattr(settings, "MINIO_SECRET_KEY", "")
    if not endpoint or not access_key:
        return {"status": "unknown", "detail": "MinIO not configured (storage profile off)"}
    import boto3
    from botocore.config import Config
    bucket = getattr(settings, "MINIO_BUCKET", "")
    # Short connect/read timeout and NO retries: a health probe must fail fast,
    # not hang for ~10s on boto3's default retry stack when MinIO is absent.
    client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name="us-east-1",
        config=Config(connect_timeout=2, read_timeout=2, retries={"max_attempts": 0}),
    )
    if bucket:
        client.head_bucket(Bucket=bucket)
    else:
        client.list_buckets()
    return {"status": "up"}


def _engine_probe() -> dict[str, Any]:
    from infra.simpleaudit_package import resolve_engine_provenance

    prov = resolve_engine_provenance()
    if not prov.version:
        return {"status": "down", "detail": "SimpleAudit engine not installed"}
    return {
        "status": "up",
        "version": prov.version,
        "commit": prov.commit,
        "source": prov.source,
    }


def _model_endpoints_probe() -> dict[str, Any]:
    """Ping each registered model endpoint's base_url, grouped by connection."""
    from model_registry.models import ModelConnection

    groups: list[dict[str, Any]] = []

    # New model: connections with their models
    conns = ModelConnection.objects.filter(enabled=True).prefetch_related("models").order_by("id")
    seen_urls: set[str] = set()
    for conn in conns:
        base = (conn.base_url or "").strip()
        if not base:
            continue
        # If no API key is configured, don't ping — report "no key"
        secret_ref = (conn.secret_reference or "").strip()
        api_key = os.environ.get(secret_ref, "") if secret_ref else ""
        if not api_key:
            conn_status = "no_key"
            latency = None
            detail = "No API key configured"
        else:
            # Ping the connection once with auth
            conn_status = "down"
            latency = None
            detail = ""
            try:
                import requests
                start = _now_ms()
                url = base.rstrip("/")
                if not url.endswith("/models"):
                    url = f"{url}/models"
                headers = {"Authorization": f"Bearer {api_key}"}
                resp = requests.get(url, timeout=2, headers=headers)
                if resp.status_code == 200:
                    conn_status = "up"
                    latency = round(_now_ms() - start, 1)
                else:
                    detail = f"HTTP {resp.status_code}"
            except Exception as exc:  # noqa: BLE001
                detail = f"{type(exc).__name__}"

        models = [
            {"id": m.id, "display_name": m.display_name, "model_id": m.model_id, "status": conn_status}
            for m in conn.models.filter(enabled=True).order_by("display_name")
        ]
        if models:
            groups.append({
                "name": conn.name,
                "provider": conn.provider,
                "base_url": base,
                "status": conn_status,
                "latency_ms": latency,
                "detail": detail,
                "models": models,
            })
        seen_urls.add(base)

    # "no_key" is not a failure — it's an expected state for unconfigured connections
    overall = "up" if all(g["status"] in ("up", "no_key") for g in groups) else "down"
    return {"status": overall, "groups": groups}


# ─── Resource probes (host + process) ────────────────────────────────────────

def _read_proc_meminfo() -> dict[str, int] | None:
    try:
        info: dict[str, int] = {}
        with open("/proc/meminfo", encoding="ascii") as fh:
            for line in fh:
                key, _, rest = line.partition(":")
                parts = rest.split()
                if parts:
                    info[key.strip()] = int(parts[0])  # values are in kB
        return info
    except (OSError, ValueError):
        return None


def _process_rss_bytes(pid: int) -> int | None:
    try:
        with open(f"/proc/{pid}/statm", encoding="ascii") as fh:
            fields = fh.read().split()
        # statm: size resident shared ... (pages). resident * page_size = RSS.
        resident_pages = int(fields[1])
        page_size = os.sysconf("SC_PAGE_SIZE")
        return resident_pages * page_size
    except (OSError, ValueError, IndexError):
        return None


def _memory_probe() -> dict[str, Any]:
    meminfo = _read_proc_meminfo()
    result: dict[str, Any] = {"status": "unknown"}
    if meminfo:
        total_kb = meminfo.get("MemTotal", 0)
        available_kb = meminfo.get("MemAvailable", meminfo.get("MemFree", 0))
        used_kb = max(total_kb - available_kb, 0)
        result.update(
            status="up",
            total_bytes=total_kb * 1024,
            used_bytes=used_kb * 1024,
            available_bytes=available_kb * 1024,
            used_percent=round(100.0 * used_kb / total_kb, 1) if total_kb else None,
        )
    # Per-process RSS for this (web) process.
    self_rss = _process_rss_bytes(os.getpid())
    if self_rss is not None:
        result["web_process_rss_bytes"] = self_rss
    return result


def _disk_probe() -> dict[str, Any]:
    try:
        usage = shutil.disk_usage("/app" if os.path.isdir("/app") else "/")
    except OSError as exc:
        return {"status": "down", "detail": str(exc)}
    total = usage.total
    return {
        "status": "up",
        "total_bytes": total,
        "used_bytes": usage.used,
        "free_bytes": usage.free,
        "used_percent": round(100.0 * usage.used / total, 1) if total else None,
    }


def _cpu_probe() -> dict[str, Any]:
    try:
        load1, load5, load15 = os.getloadavg()
    except (OSError, AttributeError):
        return {"status": "unknown", "detail": "load average unavailable"}
    try:
        cpu_count = os.cpu_count() or 1
    except Exception:  # noqa: BLE001
        cpu_count = 1
    return {
        "status": "up",
        "load_1m": round(load1, 2),
        "load_5m": round(load5, 2),
        "load_15m": round(load15, 2),
        "cpu_count": cpu_count,
        # Load per core > ~1.0 suggests CPU saturation.
        "load_per_core": round(load1 / cpu_count, 2),
    }


def _queue_throughput_probe() -> dict[str, Any]:
    from audits.models import AuditRun

    qs = AuditRun.objects.all()
    counts = {
        "queued": qs.filter(status=AuditRun.Status.QUEUED).count(),
        "active": qs.filter(
            status__in=[
                AuditRun.Status.PREPARING,
                AuditRun.Status.TARGET_EXECUTION,
                AuditRun.Status.AUDITING,
                AuditRun.Status.JUDGING,
                AuditRun.Status.AGGREGATION,
                AuditRun.Status.REPORT_GENERATION,
            ]
        ).count(),
        "completed": qs.filter(status=AuditRun.Status.COMPLETED).count(),
        "failed": qs.filter(status=AuditRun.Status.FAILED).count(),
        "cancelled": qs.filter(status=AuditRun.Status.CANCELLED).count(),
    }
    # Scenarios in flight across active runs (sum of remaining work).
    active_runs = qs.filter(
        status__in=[
            AuditRun.Status.PREPARING,
            AuditRun.Status.TARGET_EXECUTION,
            AuditRun.Status.AUDITING,
            AuditRun.Status.JUDGING,
            AuditRun.Status.AGGREGATION,
            AuditRun.Status.REPORT_GENERATION,
        ]
    )
    scenarios_in_flight = sum(
        max(r.total_scenarios - r.completed_scenarios, 0) for r in active_runs
    )
    return {"status": "up", "runs": counts, "scenarios_in_flight": scenarios_in_flight}


# ─── Aggregate ───────────────────────────────────────────────────────────────

def collect_health() -> dict[str, Any]:
    """Collect the full health snapshot for the panel.

    Returns a dict with two top-level sections:
      - ``components``: name -> probe result (web, postgres, hatchet, worker,
        minio, engine, model_endpoints)
      - ``resources``:  name -> probe result (memory, disk, cpu, queue)
    Plus a computed ``overall`` status: "ok" if nothing is down, "degraded" if
    anything is degraded/unknown-but-configured, "down" if a core component is down.
    """
    components = {
        "web": _probe(lambda: {"status": "up", "pid": os.getpid()}),
        "postgres": _probe(_postgres_probe),
        "hatchet": _probe(_hatchet_probe),
        "worker": _probe(_worker_probe),
        "minio": _probe(_minio_probe),
        "engine": _probe(_engine_probe),
        "model_endpoints": _probe(_model_endpoints_probe),
    }
    resources = {
        "memory": _probe(_memory_probe),
        "disk": _probe(_disk_probe),
        "cpu": _probe(_cpu_probe),
        "queue": _probe(_queue_throughput_probe),
    }
    return {
        "overall": _overall_status(components),
        "generated_at": time.time(),
        "components": components,
        "resources": resources,
    }


def _overall_status(components: dict[str, Any]) -> str:
    statuses = [c.get("status") for c in components.values()]
    # Core components whose absence means the system genuinely cannot audit.
    core = ("postgres", "engine")
    if any(components[name].get("status") == "down" for name in core):
        return "down"
    if "down" in statuses:
        return "degraded"
    if "degraded" in statuses:
        return "degraded"
    return "ok"
