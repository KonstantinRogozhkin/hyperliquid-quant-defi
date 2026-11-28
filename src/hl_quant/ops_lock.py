"""Atomic single-instance guard for cron wrappers: ``python -m hl_quant.ops_lock LOCKFILE -- CMD...``.

Uses a kernel ``flock`` on ``LOCKFILE``: acquisition is atomic and the lock is released by the
kernel when the process group exits for any reason (crash, SIGKILL), so there is no stale-lock
recovery step that could reclaim a lock another live invocation is still initializing.
Forwards ``SIGTERM`` and ``SIGINT`` to the child process group so ``tee`` pipelines terminate
cleanly without leaving orphan processes holding the lock descriptor.
If the lock is busy the command is skipped and the exit code is 0 (overlapping cron tick).
"""

from __future__ import annotations

import fcntl
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
from typing import Any


def _signal_pgrp(proc: subprocess.Popen[Any], sig: int) -> None:
    if proc.poll() is not None:
        return
    try:
        if hasattr(os, "killpg"):
            os.killpg(proc.pid, sig)
        else:
            proc.send_signal(sig)
    except ProcessLookupError:
        pass
    except OSError:
        try:
            proc.terminate() if sig == signal.SIGTERM else proc.kill()
        except OSError:
            pass


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
        # Spawn in a dedicated process group so SIGTERM/SIGINT reaches the whole child pipeline
        # (including `tee`), while `pass_fds=(fd,)` ensures the lock survives if this guard dies via SIGKILL.
        proc = subprocess.Popen(cmd, pass_fds=(fd,), start_new_session=True)
        prev_handlers: dict[int, Any] = {}
        received_sig: list[int] = []
        is_main = threading.current_thread() is threading.main_thread()

        def _forward_signal(signum: int, _frame: Any) -> None:
            received_sig.append(signum)
            _signal_pgrp(proc, signum)

        if is_main:
            for sig in (signal.SIGTERM, signal.SIGINT):
                prev_handlers[sig] = signal.getsignal(sig)
                signal.signal(sig, _forward_signal)
        try:
            if not is_main:
                rc = proc.wait()
            else:
                while True:
                    rc_poll = proc.poll()
                    if rc_poll is not None:
                        rc = int(rc_poll)
                        break
                    if received_sig:
                        try:
                            rc = int(proc.wait(timeout=5.0))
                        except subprocess.TimeoutExpired:
                            _signal_pgrp(proc, signal.SIGKILL)
                            rc = int(proc.wait(timeout=2.0))
                        break
                    time.sleep(0.05)
        finally:
            if is_main:
                for sig, prev in prev_handlers.items():
                    signal.signal(sig, prev)
            if hasattr(os, "killpg"):
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                except OSError:
                    pass
        if rc < 0:
            return 128 + abs(rc)
        if received_sig and rc == 0:
            return 128 + received_sig[0]
        return rc
    finally:
        os.close(fd)  # releases this process's handle on the flock


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) < 3 or args[1] != "--":
        print("usage: python -m hl_quant.ops_lock LOCKFILE -- CMD [ARGS...]", file=sys.stderr)
        return 2
    return run_locked(Path(args[0]), args[2:])


if __name__ == "__main__":
    raise SystemExit(main())
