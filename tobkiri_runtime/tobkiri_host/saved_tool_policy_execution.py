"""Retained finite saved-tool execution using explicit native policy settlement."""

from __future__ import annotations

from dataclasses import replace
import time
from typing import TYPE_CHECKING, Any, Callable, Mapping

if TYPE_CHECKING:
    from core_runtime.invocation_scope_v4 import CapturedInvocationScopeV4

from core_runtime.authority.v4 import AuthorityScope
from ecosystem.rumi_default_tools_pack.domain.tool.calculator import calculate
from ecosystem.rumi_default_tools_pack.runtime.files import _arguments as file_arguments
from tobkiri_host.host_tool_policy_types import (
    HostCapturedToolPolicy as CapturedSavedToolPolicy,
)
from tobkiri_host.models import InvocationFrame
from tobkiri_protocol.canonical import canonical_digest, canonical_json, strict_loads
from tobkiri_protocol.file_create_v1 import file_arguments as create_arguments

from .saved_tool_admission_store import HostSavedToolAdmissionStore
from .saved_tool_context import (
    SAVED_TOOL_CAPTURE_FIELDS,
    saved_tool_owner_and_request_scope,
)
from .saved_tool_consent import SavedToolConsentExecution


def requested_saved_tool_mode(invocation: Any) -> tuple[str, str]:
    """Read only the preference from the authenticated saved-turn ancestry."""
    from tobkiri_host.saved_tool_request_scope import saved_tool_request_scope

    return _requested_mode_from_scope(saved_tool_request_scope(invocation))


def _requested_mode_from_scope(scope: CapturedInvocationScopeV4) -> tuple[str, str]:
    """Read mode and turn from the scope validated in this synchronous gate."""
    request = scope.envelope.payload.get("request")
    if not isinstance(request, Mapping):
        raise PermissionError("saved tool request is unavailable")
    mode, turn = request.get("action_approval_mode", "ask"), request.get("turn_id")
    if (
        not isinstance(mode, str)
        or mode not in {"ask", "agent", "full"}
        or not isinstance(turn, str)
        or not turn
        or len(turn) > 256
    ):
        raise PermissionError("saved tool mode or turn is invalid")
    return mode, turn


class SavedToolApprovalExecution:
    """Dispatch ask to native consent and elevations to formal exact authority."""

    def __init__(
        self,
        *,
        ask: SavedToolConsentExecution,
        broker: Any,
        authority: Any,
        bind_nested: Callable[..., Any],
        entry_guard_registry: Any,
        policy_selection: Any = None,
        policy_invocation_registry: Any = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._ask, self._broker, self._authority = ask, broker, authority
        self._bind_nested, self._guards = bind_nested, entry_guard_registry
        self._clock = clock
        self._policies = policy_selection
        self._policy_invocations = policy_invocation_registry
        self._admissions = HostSavedToolAdmissionStore(authority)

    def __call__(
        self, invocation: Any, execution: Mapping[str, Any], payload: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        """Execute one exact authenticated call through its selected mode."""
        root, accepted = saved_tool_owner_and_request_scope(invocation)
        mode, turn = _requested_mode_from_scope(accepted)
        if mode == "ask":
            return self._ask(invocation, execution, payload)
        if self._policies is None:
            raise PermissionError("saved tool elevated policy settlement unavailable")
        policy = self._policies.capture_current(invocation, root)
        capture = canonical_digest({key: getattr(root, key) for key in SAVED_TOOL_CAPTURE_FIELDS})
        if not isinstance(policy, CapturedSavedToolPolicy) or (
            policy.mode != mode
            or not policy.native_boundary_digest
            or policy.turn_id != turn
            or policy.capture_digest != capture
        ):
            raise PermissionError("saved tool native policy capture changed")
        policy.assert_current()
        # Finite available descriptors only. Create delegates inner settlement
        # to the unchanged provider's guarded file workflow.
        if execution.get(
            "contract_id"
        ) != "tobkiri.service.tool.local.operation.v1" or execution.get("operation") not in {
            "rumi_default_tools_pack.files-read-operation",
            "rumi_default_tools_pack.calculator-evaluate",
            "rumi_default_tools_pack.file-create-operation",
        }:
            raise PermissionError("saved tool delegated target is unsupported")
        frozen = strict_loads(
            canonical_json({"execution": dict(execution), "payload": dict(payload)})
        )
        if execution["operation"] == "rumi_default_tools_pack.files-read-operation":
            file_arguments(frozen["payload"])
        elif execution["operation"] == "rumi_default_tools_pack.file-create-operation":
            if (
                set(frozen["payload"]) != {"tool_id", "tool_call_id", "arguments"}
                or frozen["payload"]["tool_id"] != "coding_file_create"
                or not isinstance(frozen["payload"]["tool_call_id"], str)
            ):
                raise PermissionError("saved file create payload is invalid")
            create_arguments(frozen["payload"]["arguments"])
        else:
            args = frozen["payload"].get("arguments")
            if (
                set(frozen["payload"]) != {"tool_id", "tool_call_id", "arguments"}
                or frozen["payload"]["tool_id"] != "calculator"
                or not isinstance(args, dict)
                or set(args) != {"expression"}
            ):
                raise PermissionError("saved tool calculator arguments are invalid")
            calculate(args["expression"])
        operation_digest = canonical_digest(frozen)
        reservation = self._admissions.reserve(
            {
                "capture_digest": capture,
                "turn_id": turn,
                "tool_call_id": payload["tool_call_id"],
                "owner_principal": root.caller_principal.value,
                "owner_session": root.caller_session_id,
                "operation_digest": operation_digest,
            }
        )
        nested = self._bind_nested(
            invocation, execution["contract_id"], execution["operation"], payload
        )
        try:
            SavedToolConsentExecution._validate_nested(nested, invocation, root)
            prepared = self._broker.prepare(
                InvocationFrame(
                    contract_id=execution["contract_id"],
                    version_range=None,
                    operation_id=execution["operation"],
                    payload=frozen["payload"],
                ),
                nested.context,
            )
            binding = prepared.binding
            function_id = binding.function.function_id
            if execution.get("provider_instance_id") not in {
                function_id,
                binding.artifact.pack_id + "." + function_id,
                function_id.removeprefix(binding.artifact.pack_id + "."),
            } or (binding.operation.contract_id, binding.operation.operation_id) != (
                execution["contract_id"],
                execution["operation"],
            ):
                raise PermissionError("saved tool delegated binding changed")
            snapshot = prepared.to_snapshot()
            end = self._clock() + max(0, invocation.envelope.deadline_monotonic - time.monotonic())
            retained_digest = canonical_digest(snapshot.to_dict())

            def before_settlement() -> None:
                invocation.assert_current()
                if invocation.envelope.cancellation_requested.is_set():
                    raise PermissionError("saved tool operation was cancelled")
                current_root, current_scope = saved_tool_owner_and_request_scope(
                    invocation
                )
                if current_root != root:
                    raise PermissionError("saved tool authenticated root changed")
                if _requested_mode_from_scope(current_scope) != (mode, turn):
                    raise PermissionError("saved tool mode changed")
                if (
                    strict_loads(
                        canonical_json({"execution": dict(execution), "payload": dict(payload)})
                    )
                    != frozen
                ):
                    raise PermissionError("saved tool delegated arguments changed")
                current = self._policies.capture_current(invocation, root)
                if not isinstance(current, CapturedSavedToolPolicy) or any(
                    getattr(current, key) != getattr(policy, key)
                    for key in (
                        "mode",
                        "selection_id",
                        "workspace_root",
                        "capture_digest",
                        "turn_id",
                        "native_boundary_digest",
                    )
                ):
                    raise PermissionError("saved tool native policy changed")
                current.assert_current()
                policy.assert_current()

            before_settlement()
            if execution["operation"] == "rumi_default_tools_pack.file-create-operation":
                if self._policy_invocations is None:
                    raise PermissionError("selected file-create policy is unavailable")
                reservation = self._admissions.admit_selected_policy(
                    reservation,
                    {
                        "retained_operation_digest": retained_digest,
                        "selection_id": policy.selection_id,
                        "native_boundary_digest": policy.native_boundary_digest,
                        "mode": mode,
                    },
                )
                before_settlement()
                self._admissions.claim(reservation, retained_digest)
                # This admission is not write authority. The exact native file
                # provider preserves jail/preimage checks, then settles its
                # inner fs.write.commit through the dedicated policy controller.
                with self._guards.register(
                    nested.context, snapshot.request_digest, before_settlement
                ):
                    with self._policy_invocations.register(
                        nested.context,
                        snapshot.request_digest,
                        policy,
                        before_settlement,
                    ):
                        return self._broker.invoke_prepared(
                            snapshot,
                            nested.context,
                            nested.ceiling,
                            execute_not_after_wall=end,
                            wall_clock=self._clock,
                            cancellation_requested=invocation.envelope.cancellation_requested,
                            nested_cancellation_proof=nested.cancellation_proof,
                            inline_parent_scope=nested.inline_parent_scope,
                            execution_guard=before_settlement,
                            parent_deadline_monotonic=invocation.envelope.deadline_monotonic,
                        )
            ceiling = AuthorityScope.from_dict(nested.ceiling)
            exact_scope = replace(ceiling, exact_request_digest=snapshot.request_digest)
            if not exact_scope.is_subset_of(ceiling):
                raise PermissionError("saved tool policy exceeds signed ceiling")
            settlement = self._authority.settle_policy_derived_operation(
                selection_id=policy.selection_id,
                prepared_snapshot=snapshot.to_dict(),
                request_context=nested.context,
                binding=binding,
                base_ceiling=exact_scope.to_dict(),
                workspace_root=policy.workspace_root,
                assert_current=before_settlement,
                parent_lease=invocation.envelope.lease,
            )
            # Formal authority adapter creates and verifies tagged exact Grant;
            # no client approval, model verdict, or generic bool grants authority.
            if not callable(getattr(settlement, "assert_current", None)):
                raise PermissionError("derived operation proof unavailable")

            def execution_guard() -> None:
                before_settlement()
                settlement.assert_current()

            execution_guard()
            reservation = self._admissions.admit_policy(
                reservation,
                {
                    "retained_operation_digest": retained_digest,
                    "selection_id": policy.selection_id,
                    "grant_id": settlement.grant_id,
                    "operation_capture_digest": settlement.operation_capture_digest,
                    "mode": mode,
                },
            )
            self._admissions.claim(reservation, retained_digest)
            with self._guards.register(nested.context, snapshot.request_digest, execution_guard):
                return self._broker.invoke_prepared(
                    snapshot,
                    nested.context,
                    settlement.scope,
                    execute_not_after_wall=end,
                    wall_clock=self._clock,
                    cancellation_requested=invocation.envelope.cancellation_requested,
                    nested_cancellation_proof=nested.cancellation_proof,
                    inline_parent_scope=nested.inline_parent_scope,
                    execution_guard=execution_guard,
                    parent_deadline_monotonic=invocation.envelope.deadline_monotonic,
                )
        finally:
            nested.release()
