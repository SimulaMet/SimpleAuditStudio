"""Ownership-safe local stack lifecycle shared by ``manage.py dev`` and ``spin``.

This module deliberately imports neither Django nor chat: the instance must be
resolved BEFORE settings/migrations have any side effects. No port-conflict path
may kill an unrelated process.
"""
from __future__ import annotations

import atexit
import hashlib
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import psutil

ACTIONS = frozenset({"status", "down", "reset", "uninstall", "restart", "help", "agent-info"})
_OWNER = None


def state_root() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME", "~/.local/state")).expanduser() / "simpleaudit-studio"


def _real(path: Path | str) -> Path:
    return Path(path).expanduser().resolve()


class Instance:
    def __init__(self, mode: str, root: Path | None = None):
        if mode not in {"dev", "spin"}:
            raise ValueError("unsupported mode")
        self.mode = mode
        self.root = _real(root or Path.cwd()) if mode == "dev" else None
        self.explicit_data = bool(os.environ.get("SIMPLEAUDIT_DATA_DIR"))
        identity = str(self.root) if self.root else "spin:" + str(_real(Path(os.environ.get("SIMPLEAUDIT_DATA_DIR", "~/.simpleaudit-studio"))))
        self.key = mode + "-" + hashlib.sha256(identity.encode()).hexdigest()[:16]
        self.state = state_root() / "instances" / self.key
        if self.explicit_data:
            self.data = _real(Path(os.environ["SIMPLEAUDIT_DATA_DIR"]))
        elif mode == "spin":
            self.data = _real(Path("~/.simpleaudit-studio"))
        else:
            self.data = state_root() / "data" / self.key
        if mode == "dev":
            raw = os.environ.get("SIMPLEAUDIT_SQLITE_PATH", "dev.sqlite3")
            candidate = Path(raw)
            self.db = _real(candidate if candidate.is_absolute() else self.root / candidate)
            self.explicit_db = "SIMPLEAUDIT_SQLITE_PATH" in os.environ and self.db != self.root / "dev.sqlite3"
        else:
            self.db = self.data / "studio.sqlite3"
            self.explicit_db = False

    @property
    def record_path(self) -> Path:
        return self.state / "instance.json"

    def record(self) -> dict | None:
        try:
            value = json.loads(self.record_path.read_text())
            return value if value.get("key") == self.key else None
        except (OSError, ValueError, TypeError):
            return None


def _live(record: dict | None) -> bool:
    if not record:
        return False
    try:
        pid = int(record["pid"])
        p = psutil.Process(pid)
        running = p.is_running() and p.status() != psutil.STATUS_ZOMBIE
        fresh = abs(p.create_time() - float(record["created"])) < 1.0
        owned = not hasattr(os, "getuid") or p.uids().real == os.getuid()
        return running and fresh and owned
    except (KeyError, TypeError, ValueError, psutil.Error, OSError):
        return False


def _health(port: int) -> str:
    import urllib.error
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1.5) as response:
            return "healthy" if response.status == 200 else f"HTTP {response.status}"
    except (urllib.error.URLError, OSError, ValueError):
        return "starting or unhealthy"


def show(instance: Instance) -> None:
    record = instance.record()
    if not _live(record):
        print(f"SimpleAudit Studio ({instance.mode}): stopped; instance {instance.key}")
        return
    ports = record.get("ports", {})
    print(f"SimpleAudit Studio ({instance.mode}): {_health(ports.get('public', 8000))}")
    print(f"Instance: {instance.key}  Owner: PID {record['pid']}")
    print(f"Web: http://localhost:{ports.get('public')}  Django: {ports.get('internal')}")
    print(f"Open WebUI: {ports.get('chat')}  Data: {record.get('data')}")


def _confirm(action: str, instance: Instance, yes: bool) -> None:
    if yes:
        return
    if not sys.stdin.isatty():
        raise SystemExit(f"{action} needs confirmation; pass --yes after checking instance {instance.key}")
    answer = input(f"{action} local {instance.mode} instance {instance.key}? [y/N] ").strip().lower()
    if answer not in {"y", "yes"}:
        raise SystemExit("Cancelled.")


def _stop(instance: Instance) -> None:
    record = instance.record()
    if not _live(record):
        print("Stack is already stopped.")
        return
    p = psutil.Process(int(record["pid"]))
    # Signal *only* our verified parent. Its existing shutdown/child guards
    # are responsible for stopping child processes; never kill by port/name.
    p.send_signal(signal.SIGINT if os.name != "nt" else signal.SIGTERM)
    try:
        p.wait(timeout=25)
    except psutil.TimeoutExpired as exc:
        raise SystemExit(f"Stack PID {p.pid} did not stop; see logs, no force-kill performed") from exc
    print(f"Stopped instance {instance.key}.")


def _delete_owned(instance: Instance) -> None:
    """Remove only known instance resources; never traverse user overrides."""
    if instance.explicit_data:
        raise SystemExit("Refusing to delete custom SIMPLEAUDIT_DATA_DIR; remove it manually after checking ownership")
    if instance.explicit_db:
        raise SystemExit("Refusing to delete overridden SIMPLEAUDIT_SQLITE_PATH")
    data = _real(instance.data)
    root = _real(state_root())
    safe = data == _real(Path("~/.simpleaudit-studio")) if instance.mode == "spin" else data.is_relative_to(root / "data")
    if not safe or data.is_symlink():
        raise SystemExit(f"Refusing to delete unexpected data path: {data}")
    if data.exists():
        # SQLite checkpoints can remove -wal/-shm between rmtree's listing and
        # its unlink; tolerate files that are already gone but surface real
        # errors (e.g. permission denied) instead of aborting mid-delete.
        def _rm_tolerate(fn, path, exc_info):
            if not isinstance(exc_info[1], FileNotFoundError):
                raise exc_info[1]

        shutil.rmtree(data, onerror=_rm_tolerate)
        # Best-effort sweep in case a hot journal reappeared as the last step.
        for leftover in data.iterdir() if data.exists() else []:
            try:
                shutil.rmtree(leftover) if leftover.is_dir() else leftover.unlink()
            except FileNotFoundError:
                pass
        if data.exists() and not any(data.iterdir()):
            data.rmdir()
    if instance.mode == "dev":
        for suffix in ("", "-wal", "-shm", "-journal"):
            target = Path(str(instance.db) + suffix)
            if target.is_symlink():
                raise SystemExit(f"Refusing symlinked SQLite file: {target}")
            target.unlink(missing_ok=True)
    print("Removed instance-owned data. Repository and external results were preserved.")


class _Completed:
    """Minimal stand-in for a finished subprocess result (only returncode used)."""

    def __init__(self, returncode: int):
        self.returncode = returncode


def _kill_group(proc: subprocess.Popen) -> None:
    """Terminate then kill the whole session/group a child started in."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        proc.wait(timeout=5)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass


def _uv_cleanup(mode: str) -> None:
    uv = shutil.which("uv")
    if not uv:
        print("uv executable unavailable; package cache cleanup skipped.")
        return
    # Package-scoped cache cleanup, NEVER `uv cache clean` without a name.
    # A uvx ephemeral environment may remain: uv offers no reliable per-tool
    # selective deletion of all shared cached environments.
    #
    # Each step is hard-bounded by wall clock. `subprocess.run(timeout=)` is NOT
    # sufficient: `uv cache clean` spawns children that keep its pipes open, so
    # run()'s internal communicate() can block far past the deadline (observed
    # ~94 min vs a 120 s bound). We therefore run each step in its own session
    # and kill the whole group on a real deadline.
    def _bounded(cmd, timeout):
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        deadline = time.monotonic() + timeout
        while True:
            rc = proc.poll()
            if rc is not None:
                return _Completed(rc)
            if time.monotonic() >= deadline:
                _kill_group(proc)
                return None
            time.sleep(0.5)

    for name in ("simpleaudit-studio", "open-webui"):
        result = _bounded([uv, "cache", "clean", name], timeout=120)
        if result is None:
            print(f"Timed out clearing uv cache for {name}; left as-is.")
        elif result.returncode:
            print(f"Could not clear uv cache for {name} (exit {result.returncode}).")
    if mode == "spin":
        result = _bounded([uv, "tool", "uninstall", "simpleaudit-studio"], timeout=60)
        if result is None:
            print("Timed out removing uv tool installation; left as-is.")
        elif result.returncode == 0:
            print("Removed permanent uv tool installation.")
        else:
            print("No permanent uv tool installation removed (uvx does not install one).")
    print("Shared uvx environments or dependencies may remain; no global cache was cleared.")


def instructions(mode: str, agents: bool = False) -> None:
    prefix = "uv run manage.py dev" if mode == "dev" else "uvx --from simpleaudit-studio spin"
    print(f"""SimpleAudit Studio local lifecycle
  {prefix}                  Start or reuse existing instance
  {prefix} status           Show owner, health, ports and data
  {prefix} down             Stop; preserve all data
  {prefix} restart          Restart full instance without deleting data
  {prefix} reset            Stop, delete instance data, seed and start
  {prefix} uninstall        Stop, remove instance state and app-scoped uv caches
  {prefix} help             Full command reference
  {prefix} agent-info       Instructions for coding agents

  Destructive commands prompt (or accept --yes). Existing startup flags remain.
  --port N selects a public port; --no-chat omits Open WebUI;
  --no-browser skips opening a browser. dev: --no-worker, --no-reload.
  spin: --mock, --visualize-only, --results_dir PATH.
  `help`/`agent-info` work without importing Django or starting services.
""")
    if agents:
        print("""AGENT SAFETY POLICY
- Always check `status` first; a healthy server should be reused.
- Ordinary Django Python edits trigger built-in Django autoreload. Templates
  update on refresh; do not restart Django or the whole stack after each edit.
- Worker source is NOT hot-reloaded by the current embedded worker architecture;
  do not pretend a Django reload updates worker code. Coordinate `restart` with
  running audits (it stops the stack, preserving data).
- Never kill a process by port, process name, or broad `pkill` pattern.
- Never reset/uninstall without explicit authorization and instance ownership.
- A worktree uses separate ports, Hatchet state and Open WebUI data; do not
  run a different branch against another worktree's live Django process.
- Use `status` to discover the assigned URL instead of assuming port 8000.
- Shared PostgreSQL/multi-process configurations are not destructively managed.
""")


def act(instance: Instance, action: str | None, *, yes: bool = False) -> bool:
    """Run a management action; return True when no server should be started."""
    if action is None:
        return False
    if action in {"help", "agent-info"}:
        instructions(instance.mode, agents=action == "agent-info")
        return True
    if action == "status":
        show(instance)
        return True
    if action in {"down", "reset", "uninstall", "restart"}:
        if action in {"reset", "uninstall"}:
            _confirm(action, instance, yes)
            # Validate ALL paths before touching any running process.
            if instance.explicit_data or instance.explicit_db:
                raise SystemExit("Custom data paths cannot be reset/uninstalled automatically")
        previous = instance.record()
        if action in {"restart", "reset"} and _live(previous):
            os.environ["PORT"] = str(previous["ports"]["public"])
        _stop(instance)
        if action in {"reset", "uninstall"}:
            _delete_owned(instance)
        if action == "uninstall":
            if instance.state.is_symlink():
                raise SystemExit("Refusing to remove symlinked instance state")
            if instance.state.exists():
                shutil.rmtree(instance.state)
            _, other_data = _claimed_by_others(instance)
            if other_data:
                print("Other Studio instances remain active; skipping shared uv cache/tool cleanup.")
            else:
                _uv_cleanup(instance.mode)
            return True
        if action == "down":
            return True
        if action == "reset":
            os.environ["SIMPLEAUDIT_SKIP_LEGACY_ADOPTION"] = "1"
        return False
    raise SystemExit(f"Unknown lifecycle action: {action}")


def _lock(file: Path, blocking: bool = False):
    """Lifetime advisory lock, POSIX/macOS with Windows msvcrt fallback."""
    file.parent.mkdir(parents=True, exist_ok=True)
    f = file.open("a+b")
    try:
        if os.name == "nt":
            import msvcrt
            f.seek(0)
            try:
                msvcrt.locking(f.fileno(), msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK, 1)
            except OSError:
                f.close()
                return None
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
    except (BlockingIOError, OSError):
        f.close()
        return None
    return f


def _port_free(port: int) -> bool:
    if port < 1 or port > 65535:
        return False
    with socket.socket() as s:
        try:
            s.bind(("0.0.0.0", port))
            return True
        except OSError:
            return False


def _requested_ports(public: int) -> dict:
    return {"public": public, "internal": public + 1, "chat": public + 80, "pipelines": public + 1099}


def _claimed_by_others(instance: Instance) -> tuple[set[int], set[str]]:
    ports = set()
    data = set()
    for path in (state_root() / "instances").glob("*/instance.json"):
        if path == instance.record_path:
            continue
        try:
            record = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if _live(record):
            ports.update(record.get("ports", {}).values())
            data.add(record.get("data", ""))
    return ports, data


def start(instance: Instance, *, requested_port: int = 8000, explicit_port: bool = False) -> bool:
    """Register a local stack and export ports before any Django import.

    Returns False if another invocation already owns the instance.
    The lifetime file lock prevents duplicate starts across agent processes.
    """
    global _OWNER
    if _OWNER is not None:
        raise RuntimeError("start() called twice")
    owner_lock = _lock(instance.state / "owner.lock")
    if owner_lock is None:
        print("Existing instance owns the startup lock; not starting another stack.")
        show(instance)
        return False
    allocator = _lock(state_root() / "ports.lock", blocking=True)
    if allocator is None:
        owner_lock.close()
        raise RuntimeError("Cannot acquire port allocator lock")
    try:
        existing = instance.record()
        if _live(existing):
            print("Instance is already running, reusing it.")
            show(instance)
            owner_lock.close()
            return False
        claimed, data = _claimed_by_others(instance)
        if str(_real(instance.data)) in data:
            raise SystemExit("Another running instance owns the same data directory; choose an isolated SIMPLEAUDIT_DATA_DIR")
        baseline = requested_port
        if instance.mode == "dev" and (instance.root / ".git").is_file() and not explicit_port and requested_port == 8000:
            baseline = 8100
        if explicit_port:
            candidates = [baseline]
        else:
            candidates = range(baseline, 30000, 100)
        ports = None
        for candidate in candidates:
            p = _requested_ports(candidate)
            if all(x not in claimed and _port_free(x) for x in p.values()):
                ports = p
                break
        if ports is None:
            raise SystemExit(f"Ports unavailable for {baseline}; choose another --port")
        if not instance.explicit_data:
            os.environ["SIMPLEAUDIT_DATA_DIR"] = str(instance.data)
        # Always set consistent embedded service endpoints; `--port` refers
        # to the browser-facing URL, not internal Django or Open WebUI.
        os.environ.update({
            "PORT": str(ports["public"]),
            "SIMPLEAUDIT_CHAT_PUBLIC_PORT": str(ports["public"]),
            "SIMPLEAUDIT_CHAT_INTERNAL_PORT": str(ports["internal"]),
            "SIMPLEAUDIT_CHAT_UPSTREAM": f"http://127.0.0.1:{ports['chat']}/chat",
            "SIMPLEAUDIT_BROWSER_PORT": str(ports["public"]),
            "SIMPLEAUDIT_PIPELINES_PORT": str(ports["pipelines"]),
        })
        if instance.mode == "dev":
            os.environ["SIMPLEAUDIT_SQLITE_PATH"] = str(instance.db)
        token = uuid.uuid4().hex
        record = {"key": instance.key, "pid": os.getpid(), "created": psutil.Process().create_time(),
                  "token": token, "mode": instance.mode, "root": str(instance.root) if instance.root else None,
                  "data": str(_real(instance.data)), "db": str(instance.db), "ports": ports}
        instance.state.mkdir(parents=True, exist_ok=True)
        tmp = instance.state / ("instance-" + token + ".tmp")
        tmp.write_text(json.dumps(record, indent=2))
        tmp.replace(instance.record_path)
        def _release():
            if (instance.record() or {}).get("token") == token:
                instance.record_path.unlink(missing_ok=True)
            owner_lock.close()
        atexit.register(_release)
        _OWNER = owner_lock
        print(f"Studio {instance.mode} instance {instance.key}: http://localhost:{ports['public']}")
        return True
    except BaseException:
        owner_lock.close()
        raise
    finally:
        allocator.close()
