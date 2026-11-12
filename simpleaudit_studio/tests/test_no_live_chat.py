"""The test suite never reaches a developer's live Open WebUI.

A full test run used to overwrite the workspace model ``studio.agent-1`` of the
Open WebUI that a local ``manage.py dev`` serves on ``127.0.0.1:8080``: chat
tests switch ``chat.config.ENABLED`` on and an unmocked agent save pushed the
test agent there. These tests pin the two defenses (chat off by default under
tests, and the socket guard from ``simpleaudit_studio/no_live_chat.py``).
"""
from __future__ import annotations

import socket
from unittest import mock

from django.test import SimpleTestCase, TestCase

from chat import config as chat_config
from infra.tests.factories import AgentFactory, UserFactory
from model_registry.services import sync_agent_to_openwebui
from simpleaudit_studio import no_live_chat


def _foreign_listener() -> socket.socket:
    """A listener the guard does not know about: stands in for another
    process's Open WebUI (bypasses the own-listener bookkeeping)."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    no_live_chat._real_listen(server, 8)
    server.setblocking(False)
    return server


def _accepted_any(server: socket.socket) -> bool:
    try:
        conn, _ = server.accept()
    except BlockingIOError:
        return False
    conn.close()
    return True


class ChatOffByDefaultTests(SimpleTestCase):
    def test_chat_is_disabled_whatever_the_shell_exports(self):
        # pytest.ini forces SIMPLEAUDIT_CHAT=off, so a sourced dev .env cannot
        # switch the module on for the whole run.
        self.assertFalse(chat_config.ENABLED)


class SocketGuardTests(SimpleTestCase):
    def test_default_openwebui_port_on_loopback_is_refused(self):
        for host in ("127.0.0.1", "localhost"):
            with self.subTest(host=host), self.assertRaises(no_live_chat.LiveOpenWebUIRefused):
                socket.create_connection((host, no_live_chat.OPENWEBUI_PORT), timeout=1)

    def test_a_patched_upstream_is_refused_too(self):
        server = _foreign_listener()
        self.addCleanup(server.close)
        port = server.getsockname()[1]
        with mock.patch.object(chat_config, "UPSTREAM", f"http://127.0.0.1:{port}/chat"), \
                self.assertRaises(no_live_chat.LiveOpenWebUIRefused):
            socket.create_connection(("127.0.0.1", port), timeout=1)
        self.assertFalse(_accepted_any(server))

    def test_a_stub_server_the_test_started_stays_reachable(self):
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(server.close)
        server.bind(("127.0.0.1", 0))
        server.listen(1)  # recorded as this process's own listener
        port = server.getsockname()[1]
        with mock.patch.object(chat_config, "UPSTREAM", f"http://127.0.0.1:{port}/chat"):
            socket.create_connection(("127.0.0.1", port), timeout=1).close()

    def test_unrelated_hosts_and_ports_are_not_touched(self):
        self.assertFalse(no_live_chat.is_chat_upstream(("127.0.0.1", 5432)))
        self.assertFalse(no_live_chat.is_chat_upstream(("10.0.0.5", no_live_chat.OPENWEBUI_PORT)))
        self.assertFalse(no_live_chat.is_chat_upstream("/tmp/some.sock"))


class AgentSyncNeverReachesLiveOpenWebUITests(TestCase):
    """The original leak: an unmocked agent sync with chat switched on."""

    def test_unmocked_agent_sync_is_refused_before_it_reaches_the_server(self):
        server = _foreign_listener()
        self.addCleanup(server.close)
        port = server.getsockname()[1]
        agent = AgentFactory(name="Prompted Agent")
        with mock.patch.object(chat_config, "ENABLED", True), \
                mock.patch.object(chat_config, "UPSTREAM", f"http://127.0.0.1:{port}/chat"), \
                self.assertLogs("model_registry.services", level="ERROR"):
            status = sync_agent_to_openwebui(agent, UserFactory())
        self.assertEqual(status, "skipped")
        self.assertFalse(_accepted_any(server), "a test connection reached the Open WebUI")
