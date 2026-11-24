"""Bound a warmed diagnostic process group; never certify task cancellation."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import selectors
import subprocess
import sys
import time


def run(command: list[str], log: Path, timeout: float = 40) -> int:
    """Reap the whole managed group before returning an isolation outcome."""
    if not command or not 0 < timeout <= 40:
        raise ValueError("bounded diagnostic command required")
    if not log.is_absolute() or log.parent.resolve(strict=True) != log.parent:
        raise ValueError("physical diagnostic log parent required")
    limit = 4 * 1024 * 1024
    captured = 0
    timed_out = False
    if not hasattr(os, "waitid") or not hasattr(os, "WNOWAIT"):
        raise ValueError("non-reaping process observation is required for group isolation")
    with log.open("xb") as output:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   start_new_session=True)
        selector = None
        code = None
        deadline = time.monotonic() + timeout
        pipe_closed = False
        leader_ended = False
        try:
            selector = selectors.DefaultSelector()
            os.set_blocking(process.stdout.fileno(), False)
            selector.register(process.stdout, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    break
                for key, _ in selector.select(min(0.025, remaining)):
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        pipe_closed = True
                    else:
                        kept = chunk[:max(0, limit - captured)]
                        output.write(kept)
                        captured += len(kept)
                # Retain the leader as a zombie until group cleanup. Its PID
                # cannot be reused while an inherited pipe remains open.
                ended = os.waitid(os.P_PID, process.pid,
                                  os.WEXITED | os.WNOHANG | os.WNOWAIT)
                leader_ended = ended is not None
                if ended is not None and pipe_closed:
                    break
        finally:
            # This also stops descendants when the cargo leader exited first.
            # No poll/wait reaped that leader before this managed-group signal.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except PermissionError:
                # Darwin may report EPERM for a zombie-only process group.
                # Do not suppress inability to stop a still-live group/pipe.
                if not (leader_ended and pipe_closed):
                    raise
            code = process.wait()
            if selector is not None:
                selector.close()
            process.stdout.close()
    report = {
        "format": "tobkiri.isolated-diagnostic.v1", "diagnostic_only": True,
        "forced_process_group_exit": timed_out, "process_reaped": True,
        "individual_task_cancellation_proven": False, "returncode": code,
        "watchdog_seconds": timeout, "captured_bytes": captured,
    }
    log.with_suffix(log.suffix + ".isolation.json").write_text(json.dumps(report, indent=2))
    text = log.read_bytes()
    print(text[-16384:].decode("utf-8", errors="replace"), end="")
    print(json.dumps(report))
    return 124 if timed_out else code


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    sys.exit(run(command, args.log))
