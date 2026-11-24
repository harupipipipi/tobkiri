"""Reviewed nonroot guest driver; work exists only in a quota-bound tmpfs."""

from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
from typing import Any

from tobkiri_protocol.workspace_capsule_v1 import (
    canonical,
    digest,
    export_archive,
    import_archive,
    make_capsule,
)
from tobkiri_protocol.workspace_tree_v1 import read_work_tree

MAX_OUTPUT = 32 * 1024


def run_task(
    input_path: Path, work: Path, argv: list[str], timeout: int
) -> dict[str, Any]:
    """Run one explicit argv with bounded pipes and emit a verified portable result."""
    manifest, blobs = import_archive(input_path.read_bytes())
    if os.getuid() == 0 or not 1 <= timeout <= 120 or not argv:
        raise PermissionError("guest task requires a nonroot bounded invocation")
    if any(work.iterdir()):
        raise PermissionError("guest task requires an empty tmpfs")
    for entry in manifest["files"]:
        path = work / entry["path"]
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_bytes(blobs[entry["digest"]])
        path.chmod(0o600)
    streams = {"stdout": bytearray(), "stderr": bytearray()}
    truncated = {"stdout": False, "stderr": False}

    def drain(name: str, pipe: Any) -> None:
        while True:
            chunk = pipe.read(8192)
            if not chunk:
                break
            remaining = max(0, MAX_OUTPUT - len(streams[name]))
            streams[name].extend(chunk[:remaining])
            if len(chunk) > remaining:
                truncated[name] = True

    process = subprocess.Popen(
        argv,
        cwd=work,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        env={
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": "/tmp",
            "TMPDIR": "/tmp",
            "LANG": "C.UTF-8",
            "PYTHONDONTWRITEBYTECODE": "1",
        },
    )
    threads = [
        threading.Thread(target=drain, args=(name, getattr(process, name)), daemon=True)
        for name in streams
    ]
    for thread in threads:
        thread.start()
    timed_out = False
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
    finally:
        # Kill the entire guest process group, including surviving grandchildren.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=2)
        for thread in threads:
            thread.join(timeout=2)
    receipt = {
        "exit_code": process.returncode,
        "timed_out": timed_out,
        **{
            name: bytes(data).decode("utf-8", "replace")
            for name, data in streams.items()
        },
        **{
            name + "_truncated": truncated[name] or threads[index].is_alive()
            for index, name in enumerate(streams)
        },
    }
    if (
        timed_out
        or process.returncode != 0
        or any(thread.is_alive() for thread in threads)
    ):
        return {
            "version": "tobkiri.workspace-task-output.v1",
            "receipt": receipt,
            "archive_base64": "",
            "archive_digest": "",
        }
    files = read_work_tree(work)
    output, content = make_capsule(
        workspace_id=manifest["workspace_id"],
        profile_id=manifest["source"]["profile_id"],
        plan_digest=manifest["source"]["plan_digest"],
        revision=manifest["revision"] + 1,
        parent_digest=manifest["manifest_digest"],
        recipe_digest=manifest["recipe_digest"],
        files=files,
    )
    archive = export_archive(output, content)
    return {
        "version": "tobkiri.workspace-task-output.v1",
        "receipt": receipt,
        "archive_base64": base64.b64encode(archive).decode("ascii"),
        "archive_digest": digest(archive),
    }


def main() -> None:
    """Interpret only fixed driver options and bounded argv JSON inside the guest."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--work", required=True)
    parser.add_argument("--argv-json", required=True)
    parser.add_argument("--timeout", required=True, type=int)
    values = parser.parse_args()
    if len(values.argv_json.encode()) > 100_000:
        raise ValueError("guest argv exceeds the size limit")
    result = run_task(
        Path(values.input),
        Path(values.work),
        json.loads(values.argv_json),
        values.timeout,
    )
    sys.stdout.buffer.write(canonical(result))


if __name__ == "__main__":
    main()
