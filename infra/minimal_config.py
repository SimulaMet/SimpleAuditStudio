"""Minimal config helpers for single-process deployments.

Used by `uvx simpleaudit-studio` and the HF Space Dockerfile. In minimal
config:
- Django uses SQLite (SIMPLEAUDIT_MINIMAL=1)
- Hatchet runs in embedded mode (sidecar binary + embedded Postgres)
- The worker runs in the main thread alongside the web server
- No external services required (no standalone Postgres, no supervisord)
"""

from __future__ import annotations

import logging
import os
import tempfile
import threading
from typing import Any

logger = logging.getLogger(__name__)

# Module-level state for the embedded Hatchet client
_embedded_client: Any = None
_embedded_lock = threading.Lock()


def is_minimal_config() -> bool:
    """Return True when running in minimal config (single-process, no external services)."""
    return os.environ.get("SIMPLEAUDIT_MINIMAL", "").strip() == "1"


def start_embedded_hatchet() -> Any:
    """Start an embedded Hatchet engine and return the client.

    The sidecar binary downloads on first use (~53 MB) and caches at
    ~/.hatchet/embedded/. Subsequent starts are faster.

    Returns the Hatchet client instance. Raises on failure.
    """
    global _embedded_client
    with _embedded_lock:
        if _embedded_client is not None:
            return _embedded_client

        from hatchet_sdk import ClientConfig, EmbeddedHatchetConfig, Hatchet

        # Use a fresh temp dir per session to avoid stale postmaster.pid locks
        data_dir = tempfile.mkdtemp(prefix="simpleaudit-hatchet-pg-")
        logger.info("Embedded Hatchet data dir: %s", data_dir)

        config = ClientConfig(
            embedded=EmbeddedHatchetConfig(
                postgres_data_dir=data_dir,
                ready_timeout_seconds=120.0,
            )
        )

        print("\n⏳ Starting embedded Hatchet engine (first run may take ~15s)...")
        client = Hatchet.from_embedded(config)
        _embedded_client = client
        print("✅ Hatchet engine ready.\n")
        return client


def stop_embedded_hatchet() -> None:
    """Stop the embedded Hatchet engine cleanly."""
    global _embedded_client
    with _embedded_lock:
        if _embedded_client is not None:
            try:
                _embedded_client.stop_embedded()
            except Exception:
                logger.warning("Error stopping embedded Hatchet", exc_info=True)
            _embedded_client = None


def get_embedded_client() -> Any | None:
    """Return the running embedded Hatchet client, or None."""
    return _embedded_client
