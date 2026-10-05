"""Run a child process that exits when its Studio parent disappears.

The development host may terminate the main process without running Python's
``atexit`` handlers. Detached children must therefore observe parent death
themselves instead of relying only on next-start orphan cleanup.
"""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time


def guarded_command(command: list[str], parent_pid: int | None = None) -> list[str]:
    """Return a command wrapped with the parent-death monitor."""
    guard = os.path.abspath(__file__)
    return [
        sys.executable,
        guard,
        "--parent-pid",
        str(parent_pid or os.getpid()),
        "--",
        *command,
    ]


def _stop_group(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        process.wait(timeout=5)
    except (ProcessLookupError, PermissionError, subprocess.TimeoutExpired):
        if process.poll() is None:
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--parent-pid", type=int, required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("a command is required")

    process = subprocess.Popen(command)
    try:
        while process.poll() is None:
            if os.getppid() != args.parent_pid:
                _stop_group(process)
                return 143
            time.sleep(0.5)
    except KeyboardInterrupt:
        _stop_group(process)
        return 130
    return process.returncode or 0


if __name__ == "__main__":
    sys.exit(main())
