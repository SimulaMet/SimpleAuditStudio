"""Unit tests for the async HTTP client cleanup added in infra/trace_target.py.

These guard the fix for the worker's ``RuntimeError: Event loop is closed``
teardown noise (issue #26 / Sentry SIMPLEAUDITSTUDIO-11): the per-scenario
``asyncio.run()`` loops must close the ``AsyncOpenAI`` clients they create *on
the running loop* before it shuts down, otherwise GC finalizes them on a later
loop and their pooled transports raise ``Event loop is closed``.

The real SimpleAudit engine is imported here (as elsewhere in this suite); no
network calls are made — we only construct auditors against an unreachable
base URL and inspect whether their underlying httpx clients were closed.
"""
import asyncio
from unittest import skipUnless

from django.test import TestCase

# The engine is required for these tests; skip cleanly if it is absent so the
# suite stays green in environments where the engine is not installed.
try:  # pragma: no cover - exercised implicitly when the engine is present
    from simpleaudit.targets.base import TargetContext  # noqa: F401
    _ENGINE_AVAILABLE = True
except ImportError:  # pragma: no cover - engine absent in this environment
    _ENGINE_AVAILABLE = False

skip_no_engine = skipUnless(_ENGINE_AVAILABLE, "SimpleAudit engine not installed")


def _make_auditor():
    """Build a TraceContextModelAuditor backed by an unreachable OpenAI endpoint.

    No requests are issued; the point is to exercise client construction and
    discovery/closure, not model I/O.
    """
    from infra.trace_target import TraceContextModelAuditor

    return TraceContextModelAuditor(
        model="gpt-4o-mini",
        provider="openai",
        judge_model="gpt-4o-mini",
        judge_provider="openai",
        api_key="sk-test-dummy",
        base_url="http://127.0.0.1:9/v1",
        judge_api_key="sk-test-dummy",
        judge_base_url="http://127.0.0.1:9/v1",
        judge_prompt='Return JSON {"grade": "pass"}.',
        max_turns=1,
        show_progress=False,
    )


def _discovered(auditor):
    from infra.trace_target import _collect_async_clients

    return _collect_async_clients(auditor)


def _client_closed_state(client):
    """Return True if the client's underlying httpx transport is closed.

    ``AsyncOpenAI.close()`` closes the wrapped httpx ``AsyncClient``, which
    flips its internal ``ClientState`` to CLOSED. We read that state rather than
    re-closing (which would be idempotent but less direct). Falls back to None
    if the internal layout changes so callers can assert accordingly.
    """
    wrapper = getattr(client, "_client", None)
    state = getattr(wrapper, "_state", None)
    name = getattr(state, "name", None) or str(state)
    if name is None:
        return None
    return "CLOSED" in name.upper()


@skip_no_engine
class AsyncClientCleanupTests(TestCase):
    def test_collect_finds_closable_clients(self):
        """_collect_async_clients reaches the AsyncOpenAI/httpx wrappers."""
        auditor = _make_auditor()
        found = _discovered(auditor)
        self.assertTrue(found, "no closable async clients discovered from ModelAuditor")
        # Every discovered object must expose a coroutine close method.
        from infra.trace_target import _async_close_method

        for client in found:
            self.assertIsNotNone(_async_close_method(client))

    def test_aclose_closes_clients_in_running_loop(self):
        """aclose_auditor_clients transitions the httpx clients to CLOSED."""
        from infra.trace_target import aclose_auditor_clients

        auditor = _make_auditor()
        found = _discovered(auditor)

        async def _run():
            await aclose_auditor_clients([auditor])

        asyncio.run(_run())
        states = [_client_closed_state(c) for c in found]
        self.assertTrue(all(s is True for s in states), f"clients not all closed: {states}")

    def test_run_with_client_cleanup_closes_passed_auditors(self):
        """run_with_client_cleanup closes the auditors passed explicitly."""
        from infra.trace_target import run_with_client_cleanup

        auditor = _make_auditor()
        found = _discovered(auditor)

        async def _noop():
            return "ok"

        result = asyncio.run(run_with_client_cleanup(_noop, [auditor]))
        self.assertEqual(result, "ok")
        states = [_client_closed_state(c) for c in found]
        self.assertTrue(all(s is True for s in states), f"clients not all closed: {states}")

    def test_run_with_client_cleanup_captures_per_rep_auditors(self):
        """Auditors constructed while the coroutine runs are captured via the registry.

        This mirrors AuditExperiment.run_scenario_reps, which builds a fresh
        TraceContextModelAuditor per rep (via the monkey-patched module class);
        those instances self-register into the contextvar and must be closed.
        """
        from infra.trace_target import run_with_client_cleanup

        created = []
        orig_init = None
        try:
            from infra.trace_target import TraceContextModelAuditor

            orig_init = TraceContextModelAuditor.__init__

            def _spy_init(self, *args, **kwargs):
                orig_init(self, *args, **kwargs)
                created.append(self)

            TraceContextModelAuditor.__init__ = _spy_init

            async def _simulate_reps():
                # Build two fresh auditors inside the cleanup scope, as the
                # multi-rep path does per repetition.
                for _ in range(2):
                    _make_auditor()
                return "done"

            result = asyncio.run(run_with_client_cleanup(_simulate_reps, [None]))
        finally:
            if orig_init is not None:
                TraceContextModelAuditor.__init__ = orig_init

        self.assertEqual(result, "done")
        self.assertEqual(len(created), 2, "expected two per-rep auditors to be captured")
        for auditor in created:
            states = [_client_closed_state(c) for c in _discovered(auditor)]
            self.assertTrue(
                all(s is True for s in states),
                f"per-rep auditor clients not all closed: {states}",
            )

    def test_close_is_idempotent(self):
        """Closing already-closed clients must not raise (registry overlap safety)."""
        from infra.trace_target import aclose_auditor_clients

        auditor = _make_auditor()

        async def _run_twice():
            await aclose_auditor_clients([auditor])
            await aclose_auditor_clients([auditor])  # second pass must be safe

        # Must complete without raising.
        asyncio.run(_run_twice())

    def test_none_and_empty_are_tolerated(self):
        """aclose_auditor_clients tolerates None entries and empty lists."""
        from infra.trace_target import aclose_auditor_clients

        async def _run():
            await aclose_auditor_clients([])
            await aclose_auditor_clients([None, None])

        asyncio.run(_run())
