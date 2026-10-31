"""CI-only, read-only metadata observation of the failed original first review.

No original function is replaced, no fixture/store path is created or cleaned,
and no file content is read. Profiling changes timing: this is a diagnostic,
not a native execution, concurrency proof or parity qualification.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import sys

import pytest


TARGET = "test_review_and_cancel_are_read_only"
POINTS = {
    ("profile_capture.py", "prepare_bootstrap_profile_confirmation"),
    ("profile_capture.py", "_resolve_bootstrap_candidate"),
    ("profile_capture.py", "_recover_bootstrap_publication"),
    ("profile_capture.py", "_bootstrap_pack_pin_predecessor_binding"),
    ("profile_definition_store_v4.py", "__init__"),
    ("profile_definition_store_v4.py", "_locked"),
    ("profile_definition_store_v4.py", "_read_state"),
}


def inventory(root: Path) -> dict:
    """Use lstat/scandir only; never follow a symlink or inspect key contents."""
    rows, pending = [], [root]
    while pending and len(rows) < 128:
        path = pending.pop()
        try:
            value = path.lstat()
        except FileNotFoundError:
            rows.append({"path": "." if path == root else path.relative_to(root).as_posix(),
                         "kind": "absent"})
            continue
        kind = ("directory" if stat.S_ISDIR(value.st_mode) else
                "file" if stat.S_ISREG(value.st_mode) else
                "symlink" if stat.S_ISLNK(value.st_mode) else "other")
        rows.append({"path": "." if path == root else path.relative_to(root).as_posix(),
                     "kind": kind, "mode": stat.S_IMODE(value.st_mode),
                     "bytes": value.st_size if kind == "file" else None})
        if kind == "directory":
            # Fixture roots belong to this CI test. Enumeration is bounded;
            # symlink entries are recorded without descending into their target.
            with os.scandir(path) as entries:
                children = []
                for entry in entries:
                    if len(children) + len(rows) + len(pending) >= 128:
                        return {"rows": rows, "truncated": True}
                    children.append(path / entry.name)
            pending.extend(sorted(children, reverse=True))
    return {"rows": rows, "truncated": bool(pending)}


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    """Observe before teardown while leaving the original result unchanged."""
    if item.name != TARGET:
        yield
        return
    if os.environ.get("GITHUB_ACTIONS") != "true":
        raise RuntimeError("original Setup observer requires CI")
    original = Path(os.environ["TOBKIRI_ORIGINAL_SETUP_ROOT"]) / "tobkiri_runtime"
    output = Path(os.environ["TOBKIRI_ORIGINAL_SETUP_EFFECTS"])
    root = item.funcargs["tmp_path"] / "user-data"
    report = {"format": "io.tobkiri.original-setup-effects.v1", "instrumented": True,
              "observer_operations": ["lstat", "scandir"],
              "before": inventory(root), "events": [], "events_truncated": False}

    def observe(frame, event, arg):
        if event not in {"call", "return"}:
            return
        path = Path(frame.f_code.co_filename)
        if (path.name, frame.f_code.co_name) not in POINTS or not path.is_relative_to(original):
            return
        if len(report["events"]) >= 64:
            report["events_truncated"] = True
            return
        report["events"].append({"source": path.relative_to(original).as_posix(),
                                 "function": frame.f_code.co_name, "event": event,
                                 "line": frame.f_code.co_firstlineno,
                                 "state": inventory(root)})

    previous = sys.getprofile()
    sys.setprofile(observe)
    try:
        yield
    finally:
        sys.setprofile(previous)
        report["after"] = inventory(root)
        data = json.dumps(report, indent=2) + "\n"
        if len(data.encode()) > 128 * 1024:
            raise RuntimeError("original Setup effect diagnostic exceeds bound")
        # Only the declared CI evidence directory is written. The observed
        # user-data root is never created, repaired or removed by this plugin.
        output.write_text(data)
