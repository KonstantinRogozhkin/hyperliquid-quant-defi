"""Atomic single-instance guard for cron wrappers: ``python -m hl_quant.ops_lock LOCKFILE -- CMD...``.

Uses a kernel ``flock`` on ``LOCKFILE``: acquisition is atomic and the lock is released by the
kernel when this process exits for any reason (crash, SIGKILL), so there is no stale-lock
recovery step that could reclaim a lock another live invocation is still initializing.
If the lock is busy the command is skipped and the exit code is 0 (overlapping cron tick).
"""

from __future__ import annotations

import fcntl
import os
from pathlib import Path
import subprocess
import sys


def run_locked(lock_path: Path, cmd: list[str]) -> int:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        # The child inherits the locked descriptor, so the lock survives until the protected
        # command exits even if this guard process is killed first.
        return subprocess.call(cmd, pass_fds=(fd,))
    finally:
        os.close(fd)  # releases the flock


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) < 3 or args[1] != "--":
        print("usage: python -m hl_quant.ops_lock LOCKFILE -- CMD [ARGS...]", file=sys.stderr)
        return 2
    return run_locked(Path(args[0]), args[2:])


if __name__ == "__main__":
    raise SystemExit(main())
