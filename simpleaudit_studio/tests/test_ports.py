"""Port-occupant detection and the force-kill / free-port decision.

Run:
    SIMPLEAUDIT_LOCAL_SQLITE=1 uv run manage.py test simpleaudit_studio.tests.test_ports

Every listener is a *child process*: the test runner's own command line
contains ``simpleaudit_studio`` (``manage.py test simpleaudit_studio...``),
so an in-process socket would be misclassified as a Studio run.
"""
import socket
import subprocess
import sys
import time

from django.test import SimpleTestCase

from simpleaudit_studio import ports


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


_LISTENER_SCRIPT = (
    "import socket, time\n"
    "s = socket.socket()\n"
    "s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
    "s.bind(('127.0.0.1', {port}))\n"
    "s.listen(1)\n"
    "time.sleep(60)\n"
)


def _listener_proc(port: int, marker: str = "") -> subprocess.Popen:
    """A child process holding ``port``; ``marker`` is put in its command
    line so ``_classify_command`` sees it (the way a real run's does)."""
    script = _LISTENER_SCRIPT.format(port=port)
    if marker:
        script = f"# {marker}\n" + script
    return subprocess.Popen(
        [sys.executable, "-c", script],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def _wait_port_owned(port: int, timeout: float = 15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        owner = ports.find_port_owner(port)
        if owner is not None:
            return owner
        time.sleep(0.1)
    raise AssertionError(f"port {port} never became occupied")


def _wait_port_free(port: int, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if ports.find_port_owner(port) is None:
            return
        time.sleep(0.1)
    raise AssertionError(f"port {port} still occupied after {timeout}s")


class FindPortOwnerTests(SimpleTestCase):
    def test_a_free_port_has_no_owner(self):
        self.assertIsNone(ports.find_port_owner(_free_port()))

    def test_a_plain_socket_is_unknown(self):
        port = _free_port()
        proc = _listener_proc(port)
        try:
            owner = _wait_port_owned(port)
            self.assertEqual(owner.kind, "unknown")
            self.assertEqual(owner.pid, proc.pid)
        finally:
            proc.kill()
            proc.wait()

    def test_a_studio_process_is_classified(self):
        port = _free_port()
        proc = _listener_proc(port, marker="simpleaudit_studio")
        try:
            owner = _wait_port_owned(port)
            self.assertEqual(owner.kind, "studio")
            self.assertEqual(owner.pid, proc.pid)
        finally:
            proc.kill()
            proc.wait()

    def test_an_open_webui_process_is_classified(self):
        port = _free_port()
        proc = _listener_proc(port, marker="open-webui")
        try:
            owner = _wait_port_owned(port)
            self.assertEqual(owner.kind, "open_webui")
        finally:
            proc.kill()
            proc.wait()

    def test_a_hatchet_sidecar_is_classified(self):
        port = _free_port()
        proc = _listener_proc(port, marker="hatchet-embedded-sidecar")
        try:
            owner = _wait_port_owned(port)
            self.assertEqual(owner.kind, "hatchet_sidecar")
        finally:
            proc.kill()
            proc.wait()


class KillPortOwnerTests(SimpleTestCase):
    def test_kills_the_process_and_its_children(self):
        port = _free_port()
        # A parent that spawns a child which is not in its process group —
        # the shape of a Studio run (its Open WebUI / sidecar children).
        proc = subprocess.Popen(
            [sys.executable, "-c",
             ("import socket, subprocess, sys, time\n"
              "s = socket.socket()\n"
              "s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
              f"s.bind(('127.0.0.1', {port})); s.listen(1)\n"
              "c = subprocess.Popen([sys.executable, '-c', "
              "'import time; time.sleep(60)'], start_new_session=True)\n"
              "time.sleep(60)")],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            owner = _wait_port_owned(port)
            children = ports._children_of(proc.pid)
            self.assertEqual(len(children), 1)
            self.assertTrue(ports.kill_port_owner(owner))
            _wait_port_free(port)
            self.assertFalse(ports._pid_alive(children[0]))
        finally:
            proc.kill()
            proc.wait()


class ResolvePortConflictTests(SimpleTestCase):
    def _studio_occupant(self, port: int) -> subprocess.Popen:
        return _listener_proc(port, marker="simpleaudit_studio")

    def test_a_free_port_passes_through(self):
        port = _free_port()
        self.assertEqual(
            ports.resolve_port_conflict(port, "the web server", "spin --port N",
                                        force_kill=True, yes=True),
            port,
        )

    def test_studio_occupant_is_killed_with_yes(self):
        port = _free_port()
        proc = self._studio_occupant(port)
        try:
            _wait_port_owned(port)
            result = ports.resolve_port_conflict(
                port, "the web server", "spin --port N",
                force_kill=True, yes=True,
            )
            self.assertEqual(result, port)
            _wait_port_free(port)
        finally:
            proc.kill()
            proc.wait()

    def test_no_force_kill_exits_with_a_free_port(self):
        port = _free_port()
        proc = self._studio_occupant(port)
        try:
            _wait_port_owned(port)
            with self.assertRaises(SystemExit) as ctx:
                ports.resolve_port_conflict(
                    port, "the web server", "spin --port N",
                    force_kill=False, yes=True,
                )
            self.assertEqual(ctx.exception.code, 1)
            self.assertTrue(ports._pid_alive(proc.pid))   # left alone
        finally:
            proc.kill()
            proc.wait()

    def test_a_declined_offer_exits(self):
        port = _free_port()
        proc = self._studio_occupant(port)
        try:
            _wait_port_owned(port)
            with self._patched_input("n"), self.assertRaises(SystemExit) as ctx:
                ports.resolve_port_conflict(
                    port, "the web server", "spin --port N",
                    force_kill=True, yes=False,
                )
            self.assertEqual(ctx.exception.code, 1)
            self.assertTrue(ports._pid_alive(proc.pid))
        finally:
            proc.kill()
            proc.wait()

    def test_an_accepted_offer_kills(self):
        port = _free_port()
        proc = self._studio_occupant(port)
        try:
            _wait_port_owned(port)
            with self._patched_input("y"):
                result = ports.resolve_port_conflict(
                    port, "the web server", "spin --port N",
                    force_kill=True, yes=False,
                )
            self.assertEqual(result, port)
            _wait_port_free(port)
        finally:
            proc.kill()
            proc.wait()

    def test_an_unknown_occupant_is_never_killed(self):
        port = _free_port()
        proc = _listener_proc(port)   # no marker — not ours
        try:
            _wait_port_owned(port)
            with self.assertRaises(SystemExit) as ctx:
                ports.resolve_port_conflict(
                    port, "the web server", "spin --port N",
                    force_kill=True, yes=True,
                )
            self.assertEqual(ctx.exception.code, 1)
            self.assertTrue(ports._pid_alive(proc.pid))   # not ours to touch
        finally:
            proc.kill()
            proc.wait()

    @staticmethod
    def _patched_input(answer: str):
        from unittest.mock import patch
        return patch("builtins.input", return_value=answer)


class FindFreePortTests(SimpleTestCase):
    def test_it_returns_a_free_port(self):
        port = ports.find_free_port(_free_port())
        self.assertIsNotNone(port)
        self.assertTrue(ports._port_free(port))
