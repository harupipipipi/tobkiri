"""Pure guard traversal regressions; these are not production capture proof."""

import ast
from contextvars import copy_context
import importlib.util
from pathlib import Path
import sys
from threading import Thread
from types import SimpleNamespace

import pytest

RUNTIME_ROOT = Path(__file__).resolve().parents[1]
if not (RUNTIME_ROOT / "core_runtime").is_dir():
    RUNTIME_ROOT = RUNTIME_ROOT / "work/tobkiri_runtime"
ROOT = RUNTIME_ROOT / "core_runtime"
SPEC = importlib.util.spec_from_file_location(
    "test_capture_guard_traversal_helper", ROOT / "capture_guard_traversal_v4.py"
)
helper = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = helper
SPEC.loader.exec_module(helper)


def harness():
    counts = {"capture": 0, "lease": []}
    state = {"epoch": 1, "root": 1, "provider": 1, "revoked": set()}

    def raw():
        counts["capture"] += 1
        if any(state[name] != 1 for name in ("epoch", "root", "provider")):
            raise PermissionError("capture changed")

    def capture():
        helper._check_shared_capture(raw)

    def lease(envelope, authority):
        counts["lease"].append(envelope)
        if id(envelope) in state["revoked"]:
            raise PermissionError("grant revoked")

    tree = ast.parse((ROOT / "bootstrap/production_v4.py").read_text())
    actual = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "capture_invocation_scope"
    )
    namespace = {
        "Any": object,
        "assert_dispatched_invocation": lease,
        "authority_store": object(),
        "dispatch_holder": [SimpleNamespace(assert_current=capture)],
        "assert_current_capture": capture,
        "AuthorityDenied": PermissionError,
        "_capture_guard_traversal": helper._capture_guard_traversal,
        "CapturedInvocationScopeV4": lambda envelope, guard, parent: SimpleNamespace(
            envelope=envelope, assert_current=guard, parent=parent
        ),
    }
    from threading import RLock

    namespace["caller_session_bindings_lock"] = RLock()
    parent = [None]
    namespace["parent_invocation_scopes"] = SimpleNamespace(
        lookup=lambda session: parent[0]
    )
    exec(
        compile(ast.Module(body=[actual], type_ignores=[]), "actual_guard", "exec"),
        namespace,
    )

    def make(name):
        envelope = SimpleNamespace(context=SimpleNamespace(caller_session_id=name))
        scope = namespace["capture_invocation_scope"](envelope)
        parent[0] = scope
        return scope

    return counts, state, capture, make


def test_actual_scope_recursion_keeps_every_lease_and_two_capture_checks():
    counts, _, _, make = harness()
    for index in range(8):
        scope = make(str(index))
    scope.assert_current()
    assert len(counts["lease"]) == 8
    assert counts["capture"] == 2
    scope.assert_current()
    assert len(counts["lease"]) == 16
    assert counts["capture"] == 4


@pytest.mark.parametrize("field", ["epoch", "root", "provider"])
def test_changed_capture_during_guard_is_rejected_on_exit(field):
    counts, state, capture, _ = harness()
    with pytest.raises(PermissionError, match="capture changed"):
        with helper._capture_guard_traversal(capture):
            state[field] = 2
            capture()
    assert counts["capture"] == 2
    with pytest.raises(PermissionError, match="capture changed"):
        capture()
    assert counts["capture"] == 3


def test_individual_grant_revocation_is_never_shared_or_skipped():
    counts, state, capture, make = harness()
    scope = make("root")
    with helper._capture_guard_traversal(capture):
        scope.assert_current()
        state["revoked"].add(id(scope.envelope))
        with pytest.raises(PermissionError, match="grant revoked"):
            scope.assert_current()
    assert len(counts["lease"]) == 2


def test_exception_and_later_outer_call_cannot_reuse_capture():
    counts, _, capture, _ = harness()
    with pytest.raises(ValueError):
        with helper._capture_guard_traversal(capture):
            raise ValueError("guard failed")
    with helper._capture_guard_traversal(capture):
        capture()
    assert counts["capture"] == 3


def test_context_copied_during_traversal_is_invalid_after_exit():
    counts, state, capture, _ = harness()
    with helper._capture_guard_traversal(capture):
        copied = copy_context()
    state["epoch"] = 2
    with pytest.raises(PermissionError, match="capture changed"):
        copied.run(capture)
    assert counts["capture"] == 3


def test_other_thread_never_reuses_live_traversal():
    counts, _, capture, _ = harness()
    with helper._capture_guard_traversal(capture):
        copied = copy_context()
        thread = Thread(target=lambda: copied.run(capture))
        thread.start()
        thread.join()
    assert counts["capture"] == 3


def test_different_physical_capture_guard_remains_checked():
    counts, _, capture, _ = harness()
    other = []
    with helper._capture_guard_traversal(capture):
        helper._check_shared_capture(lambda: other.append(True))
    assert counts["capture"] == 2
    assert other == [True, True]
