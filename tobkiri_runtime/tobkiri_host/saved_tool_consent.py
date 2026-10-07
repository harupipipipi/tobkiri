"""Separate native consent from the existing authority to perform a saved read."""

from __future__ import annotations

from dataclasses import dataclass, replace
import time
import uuid
from typing import TYPE_CHECKING, Any, Callable, Mapping

if TYPE_CHECKING:
    from core_runtime.invocation_scope_v4 import CapturedInvocationScopeV4

from core_runtime.authority.v4 import AuthorityScope, FunctionPrincipal
from ecosystem.rumi_default_tools_pack.runtime.files import _arguments as file_arguments
from ecosystem.rumi_default_tools_pack.domain.tool.calculator import calculate
from tobkiri_host.contracts import ResolvedOperationBinding
from tobkiri_host.consumed_tool_consent import ConsumedToolConsent
from tobkiri_host.saved_tool_admission_store import HostSavedToolAdmissionStore
from tobkiri_host.models import InvocationFrame, ExecutionKind, PackageKind
from tobkiri_host.native_saved_tool_approval import (
    NativeSavedToolApprovalGate,
    PreparedNativeToolApproval,
)
from tobkiri_host.saved_tool_context import (
    NestedToolBinding,
    saved_tool_owner_and_request_scope,
    SAVED_TOOL_CAPTURE_FIELDS,
)
from tobkiri_host.ports import (
    InteractiveApprovalRequestCommand,
    InteractiveApprovalGrantAttestation,
)
from tobkiri_protocol.canonical import canonical_digest, canonical_json, strict_loads

CONSENT_CONTRACT = "tobkiri.action.host.consent.v1"
CONSENT_OPERATION = "host_consent.admit"


@dataclass(frozen=True)
class ConsentRouteAttestation:
    """A real signed captured consent route, supplied only by Host composition.

    This object must come from resolved canonical catalog/Profile bindings. It
    is never parsed from Pack data or synthesized by this adapter.
    """

    binding: ResolvedOperationBinding
    ceiling: Mapping[str, Any]

    def validate(self) -> None:
        """Check the actual signed target identity and operation semantics."""
        binding = self.binding
        if (
            (binding.operation.contract_id, binding.operation.operation_id)
            != (CONSENT_CONTRACT, CONSENT_OPERATION)
            or binding.function.function_id
            != "rumi_host_authority_bridge_pack.host-authority.host-consent"
            or binding.artifact.package_kind is not PackageKind.HOST_EXTENSION
            or binding.variant.execution_kind is not ExecutionKind.HOST_EXTENSION
            or binding.variant.backend != "tobkiri.python-host-v4"
        ):
            raise PermissionError("signed saved tool consent route is unavailable")
        principal = FunctionPrincipal(
            parent_artifact_digest=binding.artifact.digest,
            function_implementation_digest=binding.function.implementation_digest,
            function_id=binding.function.function_id,
            contract_revision_digest=binding.operation.revision_digest,
            operation_id=binding.operation.operation_id,
        )
        ceiling = AuthorityScope.from_dict(self.ceiling)
        if (
            principal.principal_id != binding.principal_ref.value
            or ceiling.capability != "operation.invoke"
            or ceiling.semantics_digest != binding.operation.revision_digest
            or ceiling.dimensions.get("contract") != (CONSENT_CONTRACT,)
            or ceiling.dimensions.get("operation") != (CONSENT_OPERATION,)
        ):
            raise PermissionError("signed saved tool consent attestation changed")


def _turn_id(invocation: Any) -> str:
    from tobkiri_host.saved_tool_request_scope import saved_tool_request_scope

    return _turn_from_scope(saved_tool_request_scope(invocation))


def _turn_from_scope(scope: CapturedInvocationScopeV4) -> str:
    """Read the turn from a scope validated in this same synchronous gate."""
    request = scope.envelope.payload.get("request")
    if isinstance(request, Mapping) and request.get("action_approval_mode", "ask") != "ask":
        raise PermissionError("saved tool elevated mode settlement is unavailable")
    value = request.get("turn_id") if isinstance(request, Mapping) else None
    if not isinstance(value, str) or not value or len(value) > 256:
        raise PermissionError("saved tool turn is unavailable")
    return value


class SavedToolConsentExecution:
    """Consume a separate native admission Grant, then the existing read Grant.

    The consent provider only echoes the admitted operation digest. Its distinct
    signed target has no file authority. The actual read retains its prepared
    Broker context and original ceiling, so no second read Grant is created.
    """

    def __init__(
        self,
        *,
        broker: Any,
        authority: Any,
        window: Any,
        route: ConsentRouteAttestation,
        bind_nested: Callable[[Any, str, str, Mapping[str, Any]], NestedToolBinding],
        consent_proof: Any,
        entry_guard_registry: Any,
        clock: Callable[[], float] = time.time,
    ) -> None:
        route.validate()
        self._broker, self._authority, self._window = broker, authority, window
        self._route, self._bind_nested, self._clock = route, bind_nested, clock
        if consent_proof is None:
            raise PermissionError("consumed consent verifier is unavailable")
        self._consent_proof = consent_proof
        if entry_guard_registry is None:
            raise PermissionError("saved tool entry guard registry is unavailable")
        self._entry_guards = entry_guard_registry
        self._admissions = HostSavedToolAdmissionStore(authority)

    def __call__(
        self, invocation: Any, execution: Mapping[str, Any], payload: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        """Use native one-use admission for one exact captured saved tool call."""
        root, accepted = saved_tool_owner_and_request_scope(invocation)
        turn = _turn_from_scope(accepted)
        frozen = strict_loads(
            canonical_json({"execution": dict(execution), "payload": dict(payload)})
        )
        operation_digest = canonical_digest(frozen)
        if execution.get(
            "contract_id"
        ) != "tobkiri.service.tool.local.operation.v1" or execution.get("operation") not in {
            "rumi_default_tools_pack.files-read-operation",
            "rumi_default_tools_pack.calculator-evaluate",
        }:
            raise PermissionError("native saved tool read route is unsupported")
        capture_digest = canonical_digest(
            {key: getattr(root, key) for key in SAVED_TOOL_CAPTURE_FIELDS}
        )
        reservation = self._admissions.reserve(
            {
                "capture_digest": capture_digest,
                "turn_id": turn,
                "tool_call_id": payload["tool_call_id"],
                "owner_principal": root.caller_principal.value,
                "owner_session": root.caller_session_id,
                "operation_digest": operation_digest,
            }
        )
        if execution["operation"] == "rumi_default_tools_pack.files-read-operation":
            file_arguments(frozen["payload"])
        else:
            if (
                set(frozen["payload"]) != {"tool_id", "tool_call_id", "arguments"}
                or frozen["payload"]["tool_id"] != "calculator"
                or not isinstance(frozen["payload"]["arguments"], dict)
                or set(frozen["payload"]["arguments"]) != {"expression"}
            ):
                raise ValueError("Calculator consent payload is invalid")
            calculate(frozen["payload"]["arguments"]["expression"])
        readable_summary = (
            f"{payload['tool_id']}: {canonical_json(dict(payload['arguments'])).decode('utf-8')}"
        )
        if len(readable_summary) > 512:
            raise PermissionError("native saved tool arguments exceed the readable approval limit")
        read = self._bind_nested(
            invocation, execution["contract_id"], execution["operation"], payload
        )
        try:
            self._validate_nested(read, invocation, root)
            prepared_read = self._broker.prepare(
                InvocationFrame(
                    contract_id=execution["contract_id"],
                    version_range=None,
                    operation_id=execution["operation"],
                    payload=frozen["payload"],
                ),
                read.context,
            )
            binding = prepared_read.binding
            function_id = binding.function.function_id
            if execution["provider_instance_id"] not in {
                function_id,
                function_id.removeprefix(binding.artifact.pack_id + "."),
            } or (binding.operation.contract_id, binding.operation.operation_id) != (
                execution["contract_id"],
                execution["operation"],
            ):
                raise PermissionError("saved tool read target changed")
            snapshot = prepared_read.to_snapshot()
            read_digest = canonical_digest(snapshot.to_dict())
            consent_payload = {
                "retained_operation_digest": read_digest,
                "operation_digest": operation_digest,
                "capture_digest": capture_digest,
                "turn_id": turn,
                "tool_call_id": payload["tool_call_id"],
            }
            consent = self._bind_nested(
                invocation, CONSENT_CONTRACT, CONSENT_OPERATION, consent_payload
            )
            try:
                self._validate_nested(consent, invocation, root)
                self._route.validate()
                prepared = self._broker.prepare(
                    InvocationFrame(
                        contract_id=CONSENT_CONTRACT,
                        version_range=None,
                        operation_id=CONSENT_OPERATION,
                        payload=consent_payload,
                    ),
                    consent.context,
                )
                if prepared.binding != self._route.binding:
                    raise PermissionError("saved tool consent target changed")
                ceiling = AuthorityScope.from_dict(consent.ceiling)
                if ceiling != AuthorityScope.from_dict(self._route.ceiling):
                    raise PermissionError("saved tool consent ceiling changed")
                owner = "saved-consent-" + uuid.uuid4().hex
                dimensions = dict(ceiling.dimensions)
                dimensions.update(
                    invocation_owner_id=(owner,),
                    caller_session_id=(consent.context.caller_session_id,),
                    plan_digest=(consent.context.plan_digest,),
                    retained_operation_digest=(read_digest,),
                )
                exact_scope = AuthorityScope(
                    capability=ceiling.capability,
                    semantics_digest=ceiling.semantics_digest,
                    dimensions=dimensions,
                    quotas=dict(ceiling.quotas),
                    exact_request_digest=prepared.request_digest,
                    opaque=ceiling.opaque,
                )
                if not exact_scope.is_subset_of(ceiling):
                    raise PermissionError("saved tool consent exceeds signed ceiling")
                end = min(
                    self._clock() + 45,
                    self._clock()
                    + max(0, invocation.envelope.deadline_monotonic - time.monotonic()),
                )
                approval_id = "saved-consent-approval-" + uuid.uuid4().hex
                command = InteractiveApprovalRequestCommand(
                    context=replace(consent.context, request_id=approval_id),
                    target_principal=prepared.binding.principal_ref,
                    request_digest=prepared.request_digest,
                    base_scope=exact_scope.to_dict(),
                    invocation_owner_id=owner,
                    presentation_owner_principal_id=root.caller_principal.value,
                    presentation_owner_session_id=root.caller_session_id,
                    caller_publisher_lineage=consent.caller_publisher_lineage,
                    target_publisher_lineage=prepared.binding.artifact.publisher_lineage,
                    expires_at=end,
                    redacted_metadata={
                        "action": payload["tool_id"],
                        "summary": readable_summary,
                        "operation_digest": operation_digest,
                        "retained_operation_digest": read_digest,
                    },
                )
                assertion = InteractiveApprovalGrantAttestation(
                    request_id=approval_id,
                    context=consent.context,
                    target_principal=command.target_principal,
                    request_digest=command.request_digest,
                    base_scope=command.base_scope,
                    invocation_owner_id=owner,
                    caller_publisher_lineage=command.caller_publisher_lineage,
                    target_publisher_lineage=command.target_publisher_lineage,
                    expires_at=end,
                )
                native = NativeSavedToolApprovalGate(
                    authority=self._authority,
                    window=self._window,
                    prepare=lambda *_args: PreparedNativeToolApproval(
                        command, assertion, operation_digest, root
                    ),
                    clock=self._clock,
                )
                native(invocation, execution, payload)
                self._guard(invocation, root, turn, frozen, execution, payload)
                consent_lease: list[str] = []

                def capture_consent_lease() -> None:
                    exact = self._authority.reserve_effect(
                        consent.context, prepared.binding, prepared.request_digest
                    )
                    if consent_lease:
                        raise PermissionError("consent lease capture was repeated")
                    consent_lease.append(exact.value)

                result = self._broker.invoke_prepared(
                    prepared.to_snapshot(),
                    consent.context,
                    exact_scope.to_dict(),
                    before_dispatch=capture_consent_lease,
                    execute_not_after_wall=end,
                    wall_clock=self._clock,
                    cancellation_requested=invocation.envelope.cancellation_requested,
                    nested_cancellation_proof=consent.cancellation_proof,
                    inline_parent_scope=consent.inline_parent_scope,
                    execution_guard=lambda: self._guard(
                        invocation, root, turn, frozen, execution, payload
                    ),
                    parent_deadline_monotonic=invocation.envelope.deadline_monotonic,
                )
                if result != {"admitted_operation_digest": read_digest}:
                    raise PermissionError("saved tool consent result changed")
                if len(consent_lease) != 1:
                    raise PermissionError("consumed consent lease is unavailable")
                proof = ConsumedToolConsent(command, consent.context, consent_lease[0], read_digest)
                self._consent_proof.assert_current(proof)

                def read_guard() -> None:
                    self._guard(invocation, root, turn, frozen, execution, payload)
                    self._consent_proof.assert_current(proof)

                reservation = self._admissions.admit(
                    reservation,
                    {
                        "retained_operation_digest": read_digest,
                        "consent_request_id": consent.context.request_id,
                        "consent_request_digest": prepared.request_digest,
                        "consent_target": prepared.binding.principal_ref.value,
                    },
                )
                # The exact receipt stays Host-only and is durably consumed
                # before file execution; retries/restarts cannot claim it again.
                self._guard(invocation, root, turn, frozen, execution, payload)
                if canonical_digest(snapshot.to_dict()) != read_digest:
                    raise PermissionError("saved tool retained read changed")
                read_guard()
                self._admissions.claim(reservation, read_digest)
                with self._entry_guards.register(read.context, snapshot.request_digest, read_guard):
                    return self._broker.invoke_prepared(
                        snapshot,
                        read.context,
                        read.ceiling,
                        execute_not_after_wall=end,
                        wall_clock=self._clock,
                        cancellation_requested=invocation.envelope.cancellation_requested,
                        nested_cancellation_proof=read.cancellation_proof,
                        inline_parent_scope=read.inline_parent_scope,
                        execution_guard=read_guard,
                        parent_deadline_monotonic=invocation.envelope.deadline_monotonic,
                    )
            finally:
                consent.release()
        finally:
            read.release()

    @staticmethod
    def _validate_nested(binding: NestedToolBinding, invocation: Any, root: Any) -> None:
        if binding.context.caller_principal != invocation.envelope.target_principal or any(
            getattr(binding.context, key) != getattr(root, key) for key in SAVED_TOOL_CAPTURE_FIELDS
        ):
            raise PermissionError("saved tool nested capture changed")

    @staticmethod
    def _guard(
        invocation: Any,
        root: Any,
        turn: str,
        frozen: Mapping[str, Any],
        execution: Mapping[str, Any],
        payload: Mapping[str, Any],
    ) -> None:
        current, accepted = saved_tool_owner_and_request_scope(invocation)
        if (
            current != root
            or _turn_from_scope(accepted) != turn
            or canonical_digest({"execution": dict(execution), "payload": dict(payload)})
            != canonical_digest(frozen)
        ):
            raise PermissionError("saved tool approval capture changed")
