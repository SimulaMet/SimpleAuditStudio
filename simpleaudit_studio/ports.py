"""Who is sitting on a port, and what to do about it.

The CLI needs a few ports (the web port, and — with chat — Open WebUI's
upstream and the proxy's). When one is taken, the useful answer depends on
*who* is taking it:

- another SimpleAudit Studio instance, or something it spawned (Open WebUI,
  the Hatchet sidecar) — ours to manage: offer to stop it and spin cleanly;
- anything else — not ours to touch: suggest a free port and the exact flag
  or environment variable that changes the conflicting one.

Port ownership and the process tree are read through ``psutil`` rather than
``lsof``/``ps``: the same ``proc_pidinfo`` restriction that makes ``lsof``
blind to other processes' sockets in some sandboxes also makes the *global*
``psutil.net_connections()`` call raise, but the per-process
``Process(pid).connections()`` call works everywhere, so we scan PIDs one at
a time and skip the ones we are not allowed to inspect.
"""
from __future__ import annotations

import os
import signal
import socket
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

import psutil

# Command-line markers. A Studio run's listener is the python process running
# the entry script (``.../bin/spin``, ``.../bin/simpleaudit-studio``) or
# ``manage.py runserver``; its children carry the derivative's name. The
# package name is matched with its underscore spelling on purpose: the
# hyphenated project name also appears in checkout paths, where it must not
# count.
_STUDIO_MARKERS = ("simpleaudit_studio", "manage.py runserver")
_STUDIO_SCRIPTS = ("/spin", "/simpleaudit-studio")
_OPEN_WEBUI_MARKERS = ("open-webui", "open_webui")
_HATCHET_MARKER = "hatchet-embedded-sidecar"

#: Occupants the CLI may stop on the user's behalf.
OURS = ("studio", "open_webui", "hatchet_sidecar")


def ensure_stack_ports(
    public_port: int,
    *,
    chat_enabled: bool,
    chat_internal_port: int,
    chat_upstream_url: str,
    force_kill: bool = True,
    yes: bool = False,
    command_hint: str = "manage.py dev --port <free port>",
) -> None:
    """Apply one port-conflict policy to every local entry point."""
    if not chat_enabled:
        resolve_port_conflict(
            public_port, "the web server", command_hint,
            force_kill=force_kill, yes=yes,
        )
        return
    resolve_port_conflict(
        public_port, "the chat front door", command_hint,
        force_kill=force_kill, yes=yes,
    )
    if chat_internal_port != public_port:
        resolve_port_conflict(
            chat_internal_port, "the web server (internal)",
            "SIMPLEAUDIT_CHAT_INTERNAL_PORT=<free port>",
            force_kill=force_kill, yes=yes,
        )
    resolve_port_conflict(
        urlsplit(chat_upstream_url).port or 8080, "chat's Open WebUI",
        "SIMPLEAUDIT_CHAT_UPSTREAM=http://127.0.0.1:<free port>",
        force_kill=force_kill, yes=yes,
    )


@dataclass(frozen=True)
class PortOwner:
    """The process listening on a port, and what it looks like."""
    pid: int
    kind: str          # "studio" | "open_webui" | "hatchet_sidecar" | "unknown"
    command: str       # full command line, for display


def _listening_pid(port: int) -> int | None:
    """The PID listening on ``port``, or None when the port is free.

    Scans PIDs one at a time. The global ``psutil.net_connections()`` call
    walks every process and dies on the first one macOS refuses to inspect
    (``proc_pidinfo`` → EPERM on protected system PIDs); the per-process call
    does not, so we skip the PIDs we cannot read.
    """
    for pid in psutil.pids():
        try:
            for conn in psutil.Process(pid).connections(kind="inet"):
                if (
                    conn.laddr is not None
                    and conn.laddr[1] == port
                    and conn.status == psutil.CONN_LISTEN
                ):
                    return pid
        except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
            continue
    return None


def find_port_owner(port: int) -> PortOwner | None:
    """The process listening on ``port``, or None when the port is free."""
    pid = _listening_pid(port)
    if pid is None:
        return None
    return classify_port_owner(pid)


def classify_port_owner(pid: int) -> PortOwner | None:
    """What the process on a port is, from its command line.

    None when the PID is gone or its command line cannot be read.
    """
    try:
        cmdline = psutil.Process(pid).cmdline()
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return None
    if not cmdline:
        return None
    command = " ".join(cmdline)
    return PortOwner(pid=pid, kind=_classify_command(command), command=command)


def _classify_command(command: str) -> str:
    lowered = command.lower()
    if any(marker in lowered for marker in _STUDIO_MARKERS):
        return "studio"
    if any(token.endswith(script) for script in _STUDIO_SCRIPTS
           for token in command.split()):
        return "studio"
    if any(marker in lowered for marker in _OPEN_WEBUI_MARKERS):
        return "open_webui"
    if _HATCHET_MARKER in lowered:
        return "hatchet_sidecar"
    return "unknown"


def _children_of(pid: int) -> list[int]:
    try:
        return [child.pid for child in psutil.Process(pid).children()]
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return []


def _kill_tree(pid: int, sig: signal.Signals) -> None:
    """Signal ``pid`` and everything it spawned, deepest first.

    A Studio run's children (Open WebUI, the Hatchet sidecar) are not in its
    process group, so signalling the group would miss them; walk the tree
    instead.
    """
    tree: list[int] = []
    pending = [pid]
    while pending:
        current = pending.pop()
        tree.append(current)
        pending.extend(_children_of(current))
    for member in reversed(tree):
        try:
            os.kill(member, sig)
        except (ProcessLookupError, PermissionError):
            pass


def kill_port_owner(owner: PortOwner, grace: float = 10.0) -> bool:
    """Stop the occupant and its descendants. True when they are all gone."""
    _kill_tree(owner.pid, signal.SIGTERM)
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        if not _pid_alive(owner.pid):
            break
        time.sleep(0.2)
    if _pid_alive(owner.pid):
        _kill_tree(owner.pid, signal.SIGKILL)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and _pid_alive(owner.pid):
            time.sleep(0.2)
    return not _pid_alive(owner.pid)


def _pid_alive(pid: int) -> bool:
    """True when the process is still running.

    A zombie counts as gone: it is a dead process awaiting reaping and holds
    no ports, so for "is the occupant still there" it is not. (``os.kill(pid,
    0)`` and even ``psutil.is_running()`` both report a zombie as alive.)
    """
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False
    except psutil.AccessDenied:
        return True


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("0.0.0.0", port))
        except OSError:
            return False
    return True


def find_free_port(start: int, limit: int = 100) -> int | None:
    """The first free port at or above ``start``, or None after ``limit``."""
    for port in range(start, start + limit):
        if _port_free(port):
            return port
    return None


def resolve_port_conflict(
    port: int,
    what: str,
    hint: str,
    *,
    force_kill: bool,
    yes: bool,
) -> int:
    """Make sure ``port`` is free, or exit with the best advice.

    ``what`` is what the port is for ("the web server", "chat's Open WebUI"),
    ``hint`` is the exact flag or environment variable that moves it. When the
    occupant is another Studio instance or one of its derivatives, stopping it
    is offered (and done outright with ``--yes``); with ``--no-force-kill`` —
    or when the occupant is not ours — the run exits and points at a free port
    instead.
    """
    owner = find_port_owner(port)
    if owner is None:
        return port
    if owner.kind in OURS:
        if not force_kill:
            _exit_with_free_port(port, what, hint, owner)
        label = {
            "studio": "another SimpleAudit Studio instance",
            "open_webui": "an Open WebUI spawned by SimpleAudit Studio",
            "hatchet_sidecar": "a Hatchet sidecar spawned by SimpleAudit Studio",
        }[owner.kind]
        print(f"\n⚠  Port {port} is held by {label} (PID {owner.pid}):")
        print(f"   {owner.command}")
        if yes:
            confirmed = True
        else:
            try:
                answer = input("   Stop it and spin cleanly? [Y/n] ").strip().lower()
            except EOFError:
                answer = "y"
            confirmed = answer in ("", "y", "yes")
        if not confirmed:
            _exit_with_free_port(port, what, hint, owner)
        print(f"   Stopping PID {owner.pid} and its children...")
        if kill_port_owner(owner):
            print(f"   Port {port} is free. Continuing.")
            return port
        print(f"\n✗ Could not stop PID {owner.pid}.")
        print(f"  Move {what}: {hint}\n")
        raise SystemExit(1)
    _exit_with_free_port(port, what, hint, owner)


def _exit_with_free_port(port: int, what: str, hint: str, owner: PortOwner | None) -> None:
    free = find_free_port(port + 1)
    print(f"\n✗ Port {port} is in use by {what}.")
    if owner is not None:
        print(f"   PID {owner.pid}: {owner.command}")
    if free is not None:
        print(f"  A free port: {free} — move {what} with: {hint}")
    print()
    raise SystemExit(1)
