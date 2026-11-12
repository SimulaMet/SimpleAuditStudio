"""Keep test runs away from a developer's live Open WebUI.

Chat-backed tests switch ``chat.config.ENABLED`` on while ``chat.config.UPSTREAM``
keeps its default, ``http://127.0.0.1:8080/chat``. That is where a local
``manage.py dev`` serves Open WebUI, so an unmocked push from a test (an agent
save, a connection sync, a sign-in) used to overwrite that instance's real
workspace models with test rows: test agent pk 1 becomes ``studio.agent-1``, the
same id as the developer's real agent.

``install()`` patches ``socket.socket`` so that every TCP connect to the chat
upstream, or to any loopback address on Open WebUI's port, is refused, exactly
as it is in CI where nothing listens there. Ports the test process listens on
itself (stub servers) stay reachable. Both test runners install it: pytest via
``conftest.py`` and ``manage.py test`` via ``manage.py``.

Dependency-free on purpose: it runs before ``django.setup()``.
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit

#: Open WebUI's port in the default upstream (embedded and compose alike).
OPENWEBUI_PORT = 8080
_LOCAL_HOSTNAMES = frozenset({"localhost", "localhost.localdomain"})

_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex
_real_listen = socket.socket.listen

#: Every chat-upstream address a connect was refused for, in order.
refused: list[str] = []
#: Ports this process listens on itself: those reach the test's own fake.
_own_listener_ports: set[int] = set()


class LiveOpenWebUIRefused(ConnectionRefusedError):
    """A test tried to reach a real Open WebUI; mock ``chat.api.ChatAPI`` instead."""


def _is_loopback(host: str) -> bool:
    if host.lower() in _LOCAL_HOSTNAMES:
        return True
    try:
        return ipaddress.ip_address(host.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


def _upstream_host_port() -> tuple[str, int] | None:
    try:
        from chat import config as chat_config

        parts = urlsplit(chat_config.UPSTREAM)
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except Exception:  # noqa: BLE001 - the guard itself must never break a test
        return None
    return (parts.hostname or "").lower(), port


def is_chat_upstream(address: object) -> bool:
    """Whether connecting to ``address`` could reach a live Open WebUI."""
    if not (isinstance(address, tuple) and len(address) >= 2):
        return False  # AF_UNIX paths and the like
    host, port = str(address[0]).lower(), address[1]
    if port in _own_listener_ports:
        return False
    if port == OPENWEBUI_PORT and _is_loopback(host):
        return True
    upstream = _upstream_host_port()
    if upstream is None or port != upstream[1]:
        return False
    upstream_host = upstream[0]
    return host == upstream_host or (_is_loopback(host) and _is_loopback(upstream_host))


def _refuse(sock: socket.socket, address: object) -> None:
    if sock.family in (socket.AF_INET, socket.AF_INET6) and is_chat_upstream(address):
        target = f"{address[0]}:{address[1]}"
        refused.append(target)
        raise LiveOpenWebUIRefused(
            f"refused test connection to a live Open WebUI at {target}; "
            "mock chat.api.ChatAPI or httpx instead (simpleaudit_studio/no_live_chat.py)"
        )


def _guarded_connect(self, address):
    _refuse(self, address)
    return _real_connect(self, address)


def _guarded_connect_ex(self, address):
    _refuse(self, address)
    return _real_connect_ex(self, address)


def _recording_listen(self, *args):
    result = _real_listen(self, *args)
    try:
        address = self.getsockname()
    except OSError:
        return result
    if isinstance(address, tuple) and len(address) >= 2:
        _own_listener_ports.add(address[1])
    return result


def install() -> None:
    socket.socket.connect = _guarded_connect
    socket.socket.connect_ex = _guarded_connect_ex
    socket.socket.listen = _recording_listen


def uninstall() -> None:
    socket.socket.connect = _real_connect
    socket.socket.connect_ex = _real_connect_ex
    socket.socket.listen = _real_listen
