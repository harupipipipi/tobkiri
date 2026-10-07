"""Actual Host composition of native policy roots and independent reviewer."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping

from core_runtime.authority.v4 import AuthorityDenied, authority_digest
from core_runtime.native_saved_tool_policy_v4 import NativeSavedToolPolicyV4
from core_runtime.saved_tool_capture_v4 import capture_saved_tool_requested_mode
from core_runtime.saved_tool_policy_context_v4 import (
    capture_saved_tool_policy_context,
    assert_saved_native_policy_boundary,
)
from core_runtime.saved_tool_policy_roots_v4 import SavedToolPolicyRootsV4
from core_runtime.saved_tool_policy_routes_v4 import finite_saved_policy_routes
from tobkiri_host.broker import PreparedInvocationSnapshot
from tobkiri_host.models import RequestContext, OpaqueAuthorityRef
from tobkiri_host.errors import HostCoreError
from tobkiri_host.saved_tool_policy_execution import SavedToolApprovalExecution


@dataclass(frozen=True)
class ComposedSavedToolPolicyV4:
    """Narrow execution/admission/presentation ports; no authority sent to Packs."""

    execute: Any
    admit: Callable[[Any], str]
    project: Callable[..., Mapping[str, Any]]
    roots: Any


def snapshot_context(snapshot: Any) -> RequestContext:
    """Restore exact typed Host context from an authenticated Broker snapshot."""
    values = dict(snapshot.context_fingerprint)
    values["caller_principal"] = OpaqueAuthorityRef(values["caller_principal"])
    values["delegation_chain"] = tuple(
        OpaqueAuthorityRef(item) for item in values["delegation_chain"]
    )
    return RequestContext(**values)


def compose_saved_tool_policy_v4(
    *,
    broker: Any,
    authority: Any,
    store: Any,
    kernel: Any,
    catalog: Any,
    profile_id: str,
    edges: tuple,
    bindings: tuple,
    window: Any,
    ask: Any,
    bind_nested: Callable[..., Any],
    entry_guards: Any,
    policy_invocations: Any,
    selection_dispatch: Any,
    read_conversation: Callable[[Any, str], Mapping[str, Any]],
    resolve_workspace: Callable[[str, str], Any],
    selected_workspace: Callable[[], Mapping[str, Any]],
    host_context: Callable[[str], Any],
    invocation_context: Callable[[Any], Any],
    bind_nested_session: Callable[..., str],
    release_nested_session: Callable[..., None],
    context_for: Callable[..., Any],
    effect_scope_for: Callable[..., Any],
    capture_invocation_scope: Callable[..., Any],
    nested_cancellation_proof_for: Callable[..., Any],
    assert_current_capture: Callable[[], None],
    authority_records: Mapping,
    dispatch_capture: Mapping[str, Any],
) -> ComposedSavedToolPolicyV4:
    """Connect actual prepared native route, signed reviewer and finite execution."""
    from core_runtime.bootstrap.action_review_v4.reviewer_session_binder import (
        build_reviewer_capture,
    )
    from core_runtime.bootstrap.action_review_v4.reviewer_route_current import (
        capture_local_route_current,
    )
    from core_runtime.bootstrap.action_review_v4.exact_operation_review_adapter import (
        ExactOperationReviewAdapter,
    )
    from core_runtime.bootstrap.action_review_v4.reviewer_transport_authentication import (
        CommittedReviewerTransportAuthenticator,
    )
    from ecosystem.tobkiri_ui_settings_pack.runtime.settings import (
        build_reviewer_configuration_revision,
    )
    from core_runtime.authority.policy_exact_grant import CapturedReviewEvidence

    policy_binding = catalog.resolve_pinned(
        "tobkiri.action.host.approval-policy.v1", "host.action_approval_policy.select"
    )
    policy_owner = host_context("rumi_host_authority_bridge_pack.host-authority.approval-policy")
    if tuple(policy_owner.provider_bindings) != (policy_binding,):
        raise AuthorityDenied("native policy source capture unavailable")
    host_context("rumi_default_tools_pack.file-create-tool")
    host_context("rumi_host_authority_bridge_pack.host-authority.interactive-effect")
    selection_edges = tuple(
        edge
        for edge in edges
        if edge.caller.function_id == "rumi_tool_local_executor_pack.tool-executor.local"
        and edge.target.principal_id == policy_binding.principal_ref.value
    )
    if len(selection_edges) != 1 or selection_edges[0].authority_mode != "interactive_only":
        raise AuthorityDenied("signed native policy selection edge unavailable")
    conversation_edges = tuple(
        edge
        for edge in edges
        if edge.caller.function_id == "rumi_tool_local_executor_pack.tool-executor.local"
        and (
            edge.resolved_binding.operation.contract_id,
            edge.resolved_binding.operation.operation_id,
        )
        == (
            "tobkiri.resource.conversation.v1",
            "rumi_conversation_store_pack.conversation-resource",
        )
    )
    if len(conversation_edges) != 1:
        raise AuthorityDenied("signed policy conversation source edge unavailable")
    finite_edges = tuple(
        edge
        for edge in edges
        if (
            edge.caller.function_id == "rumi_tool_local_executor_pack.tool-executor.local"
            and edge.resolved_binding.operation.operation_id
            in {
                "rumi_default_tools_pack.files-read-operation",
                "rumi_default_tools_pack.calculator-evaluate",
            }
        )
        or (
            edge.caller.function_id
            == "rumi_host_authority_bridge_pack.host-authority.interactive-effect"
            and (
                edge.resolved_binding.operation.contract_id,
                edge.resolved_binding.operation.operation_id,
            )
            == ("tobkiri.service.file.create.v1", "rumi_default_tools_pack.file-create")
        )
    )
    if len(finite_edges) != 3:
        raise AuthorityDenied("native policy finite operation topology unavailable")
    generate_binding = catalog.resolve_pinned(
        "tobkiri.service.ai.generate.v1", "rumi_ai_gateway_pack.ai-gateway.generate"
    )
    generate_edges = tuple(
        edge
        for edge in edges
        if edge.caller.principal_id == policy_binding.principal_ref.value
        and edge.target.principal_id == generate_binding.principal_ref.value
        and edge.authority_mode == "profile_grant"
    )
    if len(generate_edges) != 1:
        raise AuthorityDenied("independent signed reviewer route unavailable")
    generate_edge = generate_edges[0]
    settings_context = host_context("tobkiri.ui.settings.read")
    if len(settings_context.provider_bindings) != 1:
        raise AuthorityDenied("reviewer settings owner unavailable")
    configuration_revision = build_reviewer_configuration_revision(
        settings_context,
        settings_context.provider_bindings[0],
        assert_current=assert_current_capture,
    )
    independent_captures = {}

    def independent(context: Any, edge: Any) -> Mapping[str, Any]:
        assert_current_capture()
        provider, grant = authority_records.get(edge.binding_key, (None, None))
        if (
            provider is None
            or grant is None
            or store.get_provider_authority(provider.record_id) != provider
            or store.get_grant(grant.grant_id) != grant
            or store.is_revoked("grant", grant.grant_id)
            or store.is_revoked("provider_authority", provider.record_id)
            or grant.caller.principal_id != context.caller_principal.value
            or grant.target.principal_id != edge.target.principal_id
            or grant.profile_id != context.profile_id
            or grant.activation_id != context.activation_id
            or grant.security_epoch != context.security_epoch
            or grant.profile_authority_digest != context.profile_authority_digest
        ):
            raise AuthorityDenied("independent reviewer signed authority changed")
        result = {
            "caller_principal_id": grant.caller.principal_id,
            "target_principal_id": grant.target.principal_id,
            "provider_authority_id": provider.record_id,
            "provider_authority_digest": provider.digest,
            "grant_id": grant.grant_id,
            **{
                key: getattr(context, key)
                for key in (
                    "profile_id",
                    "activation_id",
                    "activation_digest",
                    "plan_digest",
                    "profile_authority_digest",
                    "fencing_token",
                    "security_epoch",
                )
            },
        }
        independent_captures[authority_digest(result)] = result
        return result

    def reviewer_capture(invocation: Any, context: Any) -> Any:
        saved_invocation = (
            invocation_context(context.saved_scope.envelope) if context else invocation
        )
        route_current = capture_local_route_current(
            host_context("rumi_model_registry_pack.model-registry.profile"),
            host_context("rumi_provider_registry_pack.provider-registry.resource"),
            host_context("rumi_model_catalog_pack.model-catalog.bundled"),
            assert_current=assert_current_capture,
        )
        return build_reviewer_capture(
            saved_invocation,
            context,
            broker=broker,
            authority=authority,
            authority_store=store,
            catalog=catalog,
            profile_id=profile_id,
            policy_caller_binding=policy_binding,
            generate_binding=generate_binding,
            signed_generate_edge=generate_edge,
            bind_nested_session=bind_nested_session,
            release_nested_session=release_nested_session,
            context_for=context_for,
            effect_scope_for=effect_scope_for,
            capture_invocation_scope=capture_invocation_scope,
            nested_cancellation_proof_for=nested_cancellation_proof_for,
            assert_current_capture=assert_current_capture,
            local_configuration_revision=configuration_revision,
            local_route_is_current=route_current,
            capture_independent_authority=independent,
            dispatch_capture=dispatch_capture,
            parent_deadline_monotonic=saved_invocation.envelope.deadline_monotonic,
            parent_cancellation=saved_invocation.envelope.cancellation_requested,
        )

    def verify_prepared(operation: Any) -> None:
        snapshot = PreparedInvocationSnapshot.from_dict(operation.prepared_snapshot)
        broker.validate_prepared_snapshot(snapshot, snapshot_context(snapshot))

    def current_operation(operation: Any, capture: Mapping[str, Any]) -> None:
        assert_current_capture()
        verify_prepared(operation)
        selected = selected_workspace()
        if selected.get("canonical_root") != operation.workspace_root:
            raise AuthorityDenied("policy workspace current capture changed")
        routes = capture.get("delegation_boundary", {}).get("routes", [])
        if not routes or any(
            route.get("workspace_receipt")
            != {
                key: selected.get(key)
                for key in (
                    "workspace_id",
                    "canonical_root",
                    "mount_revision",
                    "root_st_dev",
                    "root_st_ino",
                )
            }
            for route in routes
        ):
            raise AuthorityDenied("policy original workspace receipt changed")

    def saved_guard(invocation: Any, root: Any, capture: Mapping[str, Any]) -> None:
        routes = capture.get("delegation_boundary", {}).get("routes", [])
        ids = {route.get("workspace_receipt", {}).get("workspace_id") for route in routes}
        if len(ids) != 1 or None in ids:
            raise AuthorityDenied("native policy workspace receipt unavailable")
        assert_saved_native_policy_boundary(
            invocation,
            root,
            capture,
            workspace_id=next(iter(ids)),
            resolve_workspace=resolve_workspace,
            selected_workspace=selected_workspace,
        )

    def review_adapter(captured: Any, context: Any) -> tuple[Any, Callable]:
        independent_capture = independent_captures.get(
            captured.native_facts["authority_capture_digest"]
        )
        if independent_capture is None:
            raise AuthorityDenied("reviewer stable authority capture unavailable")
        authenticate = CommittedReviewerTransportAuthenticator(
            store, captured.boundary, independent_capture, captured.assert_current
        )

        def verify(provenance: Mapping[str, Any]) -> None:
            authenticate(provenance)
            data = provenance.get("transport_proof", {}).get("reviewer_prepared_snapshot")
            snapshot = PreparedInvocationSnapshot.from_dict(data)
            broker.validate_prepared_snapshot(snapshot, snapshot_context(snapshot))

        adapter = ExactOperationReviewAdapter(
            captured.reviewer_port,
            captured.boundary,
            turn_id=context.turn_id,
            evidence_factory=CapturedReviewEvidence,
            authenticate_transport=verify,
        )
        return adapter, verify

    def capture_context(invocation: Any) -> Any:
        return capture_saved_tool_policy_context(
            invocation,
            profile_id=profile_id,
            read_conversation=read_conversation,
            resolve_workspace=resolve_workspace,
            selected_workspace=selected_workspace,
        )

    native = NativeSavedToolPolicyV4(
        broker=broker,
        authority=authority,
        store=store,
        kernel=kernel,
        catalog=catalog,
        window=window,
        selection_dispatch=selection_dispatch,
        bind_nested=bind_nested,
        finite_routes=lambda inv, context: finite_saved_policy_routes(
            inv,
            store=store,
            edges=edges,
            bindings=bindings,
            workspace_binding=context.workspace_binding,
        ),
        reviewer_capture=reviewer_capture,
        reviewer_adapter=review_adapter,
        prepared_validator=verify_prepared,
        current_guard=current_operation,
        saved_root_guard=saved_guard,
    )
    roots = SavedToolPolicyRootsV4(
        capture_context=capture_context, create=native, restore=native.restore
    )
    execute = SavedToolApprovalExecution(
        ask=ask,
        broker=broker,
        authority=authority,
        bind_nested=bind_nested,
        entry_guard_registry=entry_guards,
        policy_selection=roots,
        policy_invocation_registry=policy_invocations,
    )

    def admit(invocation: Any) -> str:
        mode = capture_saved_tool_requested_mode(invocation)
        if mode == "agent" and not configuration_revision()[0]:
            raise AuthorityDenied("reviewer model unconfigured")
        return mode

    def project(
        invocation: Any, conversation_id: str | None, workspace_id: str, workspace_binding: Any
    ) -> Mapping[str, Any]:
        assert_current_capture()
        modes = ["ask", "full"]
        reason = "reviewer_unavailable"
        try:
            reviewer_capture(invocation, None).assert_current()
        except (AuthorityDenied, PermissionError, HostCoreError):
            pass
        else:
            modes = ["ask", "agent", "full"]
            reason = "available"
        active = "ask"
        if conversation_id is not None:
            candidates = []
            for root in tuple(kernel.policy_roots.values()):
                capture = root.capture()
                receipt = root.receipt_record()
                if (
                    capture.get("conversation") == conversation_id
                    and capture.get("workspace") == str(workspace_binding.canonical_root)
                    and capture.get("context", {}).get("profile_id") == profile_id
                    and receipt[1].get("state") == "committed_selection"
                    and receipt[1].get("capture") == capture
                ):
                    candidates.append((root.command.expires_at, capture["mode"]))
            if candidates:
                active = max(candidates)[1]
        return {"available_modes": modes, "active_mode": active, "reason": reason}

    return ComposedSavedToolPolicyV4(execute, admit, project, roots)
