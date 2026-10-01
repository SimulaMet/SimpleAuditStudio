"""Leftovers of a hard-killed run are cleaned up before the embedded engine starts.

Regression: an orphaned sidecar is re-parented to PID 1, which is always alive,
so "parent is gone" never matched and the next start failed with
``lock file "postmaster.pid" already exists``.

Run:
    SIMPLEAUDIT_LOCAL_SQLITE=1 uv run manage.py test infra.tests.test_embedded_cleanup
"""
import os
import signal
import tempfile
from pathlib import Path
from subprocess import CompletedProcess
from unittest import mock

from django.test import SimpleTestCase

from infra import minimal_config as mc


class OrphanTests(SimpleTestCase):
    def test_reparented_to_init_is_orphaned(self):
        self.assertTrue(mc._orphaned(1))
        self.assertTrue(mc._orphaned(999_999_999))   # parent gone
        self.assertFalse(mc._orphaned(os.getppid()))   # a live parent
        with mock.patch.object(mc.os, "getpid", return_value=1):
            self.assertFalse(mc._orphaned(1))   # we are PID 1 (container): our own child

    def test_orphaned_sidecars_are_terminated(self):
        ps = "  501 1 /x/hatchet-embedded-sidecar_darwin_arm64 -handshake-file a\n" \
             "  502 777 /x/hatchet-embedded-sidecar_darwin_arm64 -handshake-file b\n"
        alive = {501: True, 502: True, 777: True}
        with mock.patch.object(mc.subprocess, "run", return_value=CompletedProcess([], 0, ps, "")), \
             mock.patch.object(mc, "_pid_alive", side_effect=lambda pid: alive.get(pid, False)), \
             mock.patch.object(mc, "_wait_gone"), \
             mock.patch.object(mc.os, "kill") as kill:
            mc._kill_stale_sidecars()
        signalled = {c.args for c in kill.call_args_list}
        self.assertIn((501, signal.SIGTERM), signalled)   # orphan (parent is init)
        self.assertNotIn((502, signal.SIGTERM), signalled)   # another live instance's sidecar


class StalePostgresTests(SimpleTestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        (self.dir / "data").mkdir()
        self.lock = self.dir / "data" / "postmaster.pid"
        self.lock.write_text("4242\n/some/data\n")

    def test_lock_of_a_dead_postgres_is_removed(self):
        with mock.patch.object(mc, "_pid_alive", return_value=False):
            mc._cleanup_stale_embedded_pg(str(self.dir))
        self.assertFalse(self.lock.exists())

    def test_orphaned_live_postgres_is_shut_down(self):
        with mock.patch.object(mc, "_pid_alive", side_effect=[True, False]), \
             mock.patch.object(mc, "_parent_pid", return_value=1), \
             mock.patch.object(mc, "_wait_gone"), \
             mock.patch.object(mc.os, "kill") as kill:
            mc._cleanup_stale_embedded_pg(str(self.dir))
        kill.assert_called_once_with(4242, signal.SIGINT)

    def test_postgres_of_a_running_instance_is_left_alone(self):
        with mock.patch.object(mc, "_pid_alive", return_value=True), \
             mock.patch.object(mc, "_parent_pid", return_value=os.getppid()), \
             mock.patch.object(mc.os, "kill") as kill:
            mc._cleanup_stale_embedded_pg(str(self.dir))
        kill.assert_not_called()
        self.assertTrue(self.lock.exists())


class EmbeddedSignalTests(SimpleTestCase):
    def test_sidecar_has_separate_process_group_and_explicit_stop(self):
        """Exercise SDK spawning with a real child, without starting Postgres."""
        import subprocess
        import sys

        from hatchet_sdk import EmbeddedHatchetConfig, embedded

        child = None
        real_popen = subprocess.Popen

        def spawn_stub(*args, **kwargs):
            nonlocal child
            child = real_popen(
                [sys.executable, "-c",
                 "import sys; sys.stdin.buffer.read()"],
                **kwargs,
            )
            return child

        handshake = embedded.Handshake(
            token="test", tenant_id="test", grpc_address="localhost:1",
            api_url="http://localhost:1",
        )
        try:
            with mock.patch.object(subprocess, "Popen", side_effect=spawn_stub), \
                 mock.patch.object(embedded, "_wait_for_handshake", return_value=handshake), \
                 mc._isolated_embedded_process():
                sidecar = embedded.start_embedded_sidecar(
                    EmbeddedHatchetConfig(binary_path=sys.executable)
                )
            self.assertNotEqual(os.getpgid(child.pid), os.getpgrp())
            self.assertIsNone(child.poll())
            sidecar.stop()
            self.assertIsNotNone(child.poll())
        finally:
            if child is not None and child.poll() is None:
                child.terminate()
                child.wait(timeout=5)
