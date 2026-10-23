"""Actual source native ceremony, signed native decision, Broker and durable root."""

import ast
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace as NS
import threading
import time

import pytest

from tests import approval_policy_fixture as fixture
from tests.test_interactive_approval_v4 import _CONTRACT, _decision_command
from core_runtime.host_contract import bind_host_contract
from core_runtime.native_saved_tool_policy_v4 import NativeSavedToolPolicyV4
from core_runtime.saved_tool_policy_selection_v4 import RetainedSelectionDispatchV4
from tobkiri_host.saved_tool_context import NestedToolBinding


def prepare_actual_native_inputs(tmp_path, monkeypatch):
    """Reuse signed fixture provisioning; stop before its manual native ceremony."""
    source = ast.parse(Path(fixture.__file__).read_text())
    function = next(
        node
        for node in source.body
        if isinstance(node, ast.FunctionDef) and node.name == "build_bounded_selection"
    )
    function.name = "native_inputs"
    cut = next(
        index
        for index, node in enumerate(function.body)
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "policy" for target in node.targets)
    )
    function.body = function.body[:cut]
    for node in ast.walk(function):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "LatePort"
        ):
            node.func.id = "RetainedSelectionDispatchV4"
    function.body.append(
        ast.parse(
            "return SimpleNamespace(harness=harness, adapter=adapter, catalog=catalog, broker=broker, context=context, boundary_routes=boundary_routes, port=port, ceiling=ceiling)"
        ).body[0]
    )
    namespace = {**fixture.__dict__, "RetainedSelectionDispatchV4": RetainedSelectionDispatchV4}
    exec(
        compile(
            ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[])),
            "actual-native-fixture-provision",
            "exec",
        ),
        namespace,
    )
    return namespace["native_inputs"](tmp_path, monkeypatch)


@pytest.mark.parametrize("decision", ["approved", "denied"])
def test_actual_firsttool_native_controller_consumes_exact_selection_or_stops(
    tmp_path, monkeypatch, decision
):
    with bind_host_contract(_CONTRACT):
        actual = prepare_actual_native_inputs(tmp_path, monkeypatch)
        actual.harness.kernel.policy_roots = {}
        actual.harness.kernel.revoke(
            target_kind="grant",
            target_id=actual.harness.grant.grant_id,
            reason="no broad native selection authorization",
        )
        state = {"released": False, "native_opened": 0}
        invocation = NS(
            assert_current=lambda: None,
            presentation_owner_principal_id=actual.harness.caller.principal_id,
            presentation_owner_session_id="session-caller",
            envelope=NS(
                deadline_monotonic=time.monotonic() + 60, cancellation_requested=threading.Event()
            ),
        )
        saved = NS(
            root=actual.context,
            mode="full",
            conversation_id="conversation-1",
            turn_id="turn-1",
            workspace_id="workspace",
            capture_digest="sha256:source",
            workspace_binding=NS(canonical_root=tmp_path.resolve()),
            assert_current=lambda: None,
            saved_scope=NS(envelope=NS(deadline_monotonic=time.monotonic() + 180)),
        )

        def open_window(command):
            state["native_opened"] += 1
            request = actual.harness.store.get_interactive_approval_request(command.request_id)
            if decision == "approved":
                native_decision = replace(
                    _decision_command(
                        actual.harness,
                        command.request_id,
                        phrase=request.redacted_metadata["confirmation_phrase"],
                    ),
                    context=actual.context,
                )
                actual.adapter.approve_interactive_approval(native_decision)
            else:
                native_decision = replace(
                    _decision_command(actual.harness, command.request_id, action="deny"),
                    context=actual.context,
                )
                actual.adapter.deny_interactive_approval(native_decision)
            return {"opened": True, "request_id": command.request_id}

        def saved_guard(current, root, capture):
            assert root is saved.root
            assert capture["owner_principal"] == actual.harness.caller.principal_id
            assert capture["owner_session"] == "session-caller"
            assert capture["conversation"] == saved.conversation_id
            assert capture["turn"] == saved.turn_id

        native = NativeSavedToolPolicyV4(
            broker=actual.broker,
            authority=actual.adapter,
            store=actual.harness.store,
            kernel=actual.harness.kernel,
            catalog=actual.catalog,
            window=NS(open_authority_approval_window=open_window),
            selection_dispatch=actual.port,
            bind_nested=lambda *args: NestedToolBinding(
                actual.context,
                actual.ceiling.to_dict(),
                actual.harness.caller.publisher_lineage
                if hasattr(actual.harness.caller, "publisher_lineage")
                else "publisher.fixture",
                None,
                lambda: state.update(released=True),
            ),
            finite_routes=lambda inv, context: actual.boundary_routes,
            reviewer_capture=lambda *args: pytest.fail("full must not run a reviewer"),
            reviewer_adapter=lambda *args: pytest.fail("full must not bind reviewer"),
            prepared_validator=lambda operation: None,
            current_guard=lambda operation, capture: None,
            saved_root_guard=saved_guard,
            clock=actual.harness.clock,
        )
        if decision == "denied":
            with pytest.raises(PermissionError, match="stopped: denied"):
                native(invocation, saved)
            assert actual.harness.kernel.policy_roots == {}
        else:
            root = native(invocation, saved)
            assert root.resolve_current() == "full"
            # Root duration follows the actual retained saved scope, not the
            # first executor deadline or the separate 45-second native wait.
            assert 1100 < root.command.expires_at <= 1180
            receipt = root.receipt_record()[1]
            assert receipt["state"] == "committed_selection"
            lease = actual.harness.store.get_lease(receipt["committed_selection_lease"])[0]
            assert actual.harness.store.grant_usage(lease.grant_id) == (0, 1)
            assert actual.harness.kernel.policy_roots[root.selection_id] is root
            assert native.restore(invocation, saved).selection_id == root.selection_id
        assert state["native_opened"] == 1 and state["released"]
