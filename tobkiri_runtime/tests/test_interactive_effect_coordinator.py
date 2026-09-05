"""Focused contracts for the Host interactive-effect coordinator transforms."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from typing import Any, Mapping

import pytest

from core_runtime.interactive_effect_coordinator import (
    CapturedInteractiveEffectRoute,
    HostInteractiveEffectService,
    INTERACTIVE_EFFECT_SPECS,
    InteractiveEffectUnavailable,
    _execute_payload,
    _presentation_metadata,
)
from core_runtime.host_provider_backend_v4 import HostProviderCaptureContextV4
from core_runtime.authority.v4 import AuthorityScope
from ecosystem.rumi_host_authority_bridge_pack.runtime import bridge
from tobkiri_host.broker import RequestEnvelope
from tobkiri_host.models import OpaqueAuthorityRef, RequestContext
from tobkiri_host.ports import InteractiveEffectStatus, OpaqueInvocationLease
from tobkiri_protocol.canonical import canonical_digest


def _git_plan(operation: str, **details: object) -> dict[str, object]:
    """Build one sealed Git prepare response accepted by the fixed transform."""

    plan: dict[str, object] = {
        "plan_version": "tobkiri.git-write.plan.v4",
        "operation": operation,
        "profile_id": "profile.nondefault",
        "workspace_id": "workspace.primary",
        "repository_root": ".",
        "expected_mount_revision": 7,
    }
    plan.update(details)
    return {**plan, "plan_digest": canonical_digest(plan)}


def test_shell_execute_transform_preserves_only_prepare_result_and_arguments() -> None:
    """Shell execution receives a coordinator-built plan, never a UI plan."""

    request = {"command": ["git", "status"], "cwd": ".", "env": {}}
    prepared = {
        "redacted_plan": {"plan_version": "tobkiri.shell-execute.plan.v4"},
        "plan_digest": canonical_digest({"plan": "shell"}),
        "executed": False,
    }

    result = _execute_payload(INTERACTIVE_EFFECT_SPECS["shell_execute"], request, prepared)

    assert result == {
        "redacted_plan": prepared["redacted_plan"],
        "plan_digest": prepared["plan_digest"],
        "arguments": request,
    }


@pytest.mark.parametrize(
    ("effect_kind", "operation"),
    [
        ("git_commit", "git-commit"),
        ("git_restore", "git-restore"),
    ],
)
def test_git_write_transforms_keep_plan_profile_and_workspace_bindings(
    effect_kind: str,
    operation: str,
) -> None:
    """Git execute must receive the same non-default profile and workspace."""

    plan = _git_plan(operation)
    result = _execute_payload(INTERACTIVE_EFFECT_SPECS[effect_kind], {}, plan)

    assert result == {
        "plan": plan,
        "profile_id": "profile.nondefault",
        "workspace_id": "workspace.primary",
    }


def test_git_patch_transform_keeps_only_prepared_plan_and_patch_bytes() -> None:
    """Patch execution carries the original patch solely for plan-hash recheck."""

    plan = _git_plan("git-apply-patch")
    result = _execute_payload(
        INTERACTIVE_EFFECT_SPECS["git_apply_patch"],
        {"patch": "diff --git a/a b/a\n"},
        plan,
    )

    assert result["plan"] == plan
    assert result["profile_id"] == "profile.nondefault"
    assert result["workspace_id"] == "workspace.primary"
    assert result["patch"] == "diff --git a/a b/a\n"


def test_git_push_transform_keeps_the_provider_sealed_plan_only() -> None:
    """Push target, remote, and credential routing cannot originate from the UI."""

    plan = {"remote_name": "origin", "workspace_id": "workspace.primary"}
    prepared = {"plan": plan, "plan_digest": canonical_digest(plan)}
    result = _execute_payload(INTERACTIVE_EFFECT_SPECS["git_push"], {}, prepared)

    assert result == prepared


def test_transform_rejects_tampered_git_plan() -> None:
    """A UI or coordinator cannot replace the result of the signed prepare edge."""

    plan = _git_plan("git-commit")
    plan["workspace_id"] = "workspace.foreign"

    with pytest.raises(InteractiveEffectUnavailable):
        _execute_payload(INTERACTIVE_EFFECT_SPECS["git_commit"], {}, plan)


def _prepared_presentation(payload: Mapping[str, Any]) -> Any:
    """Build a Host-prepared snapshot double for approval-copy coverage."""

    return SimpleNamespace(
        normalized_payload=payload,
        request_digest=canonical_digest({"prepared": payload}),
    )


@pytest.mark.parametrize(
    ("effect_kind", "payload", "expected_action", "expected_detail", "forbidden"),
    (
        (
            "shell_execute",
            {
                "redacted_plan": {"plan_version": "tobkiri.shell-execute.plan.v4"},
                "plan_digest": canonical_digest({"shell": "prepared"}),
                "arguments": {
                    "command": ["git", "push", "--token", "token-value"],
                    "cwd": "packages/app",
                },
            },
            "Run local terminal command",
            'argv: ["git", "push", "--token", "[REDACTED]"]\ncwd: packages/app',
            "token-value",
        ),
        (
            "git_commit",
            {
                "plan": _git_plan(
                    "git-commit",
                    message="Fix parser token=secret-value",
                    expected_head_ref="refs/heads/main",
                    expected_index_tree="c" * 40,
                ),
                "profile_id": "profile.nondefault",
                "workspace_id": "workspace.primary",
            },
            "Create Git commit",
            (
                "message: Fix parser [REDACTED]\nrepository: .\n"
                "branch: refs/heads/main\nstaged tree: " + "c" * 40
            ),
            "secret-value",
        ),
        (
            "git_push",
            {
                "plan": {
                    "remote_name": "origin",
                    "remote_host": "github.com",
                    "destination_ref": "refs/heads/main",
                    "force_with_lease": {
                        "mode": "exact-remote-oid",
                        "allow_non_fast_forward": False,
                        "argument": "--force-with-lease=refs/heads/main:012345",
                    },
                    "credential_transport": {"credential_handle": "must-not-display"},
                },
                "plan_digest": "",  # Filled below so each literal remains readable.
            },
            "Push Git branch",
            (
                "remote: origin (github.com)\nref: refs/heads/main\n"
                "lease target: --force-with-lease=refs/heads/main:012345\n"
                "lease mode: exact-remote-oid\nfast-forward only: yes\n"
                "non-fast-forward updates: not permitted"
            ),
            "must-not-display",
        ),
        (
            "git_apply_patch",
            {
                "plan": _git_plan(
                    "git-apply-patch",
                    paths=["src/main.py", "secrets/token=not-displayed.py"],
                    stdin_sha256="a" * 64,
                ),
                "profile_id": "profile.nondefault",
                "workspace_id": "workspace.primary",
                "patch": "raw secret patch bytes must never reach metadata",
            },
            "Apply Git patch",
            "patch: sha256:" + "a" * 64,
            "raw secret patch bytes",
        ),
        (
            "git_restore",
            {
                "plan": _git_plan(
                    "git-restore",
                    paths=["src/main.py", "bin/tool"],
                    source_tree="b" * 40,
                    targets=[
                        {"path": "src/main.py", "mode": "100644"},
                        {"path": "bin/tool", "mode": "100755"},
                    ],
                ),
                "profile_id": "profile.nondefault",
                "workspace_id": "workspace.primary",
            },
            "Restore Git paths",
            "restore mode: working tree from prepared tree " + "b" * 40,
            "postimages",
        ),
    ),
)
def test_host_approval_presentation_describes_each_prepared_high_risk_effect(
    effect_kind: str,
    payload: Mapping[str, Any],
    expected_action: str,
    expected_detail: str,
    forbidden: str,
) -> None:
    """Approval copy comes only from a bounded Host-prepared effect snapshot."""

    mutable_payload = dict(payload)
    if effect_kind == "git_push":
        plan = dict(mutable_payload["plan"])
        mutable_payload["plan"] = plan
        mutable_payload["plan_digest"] = canonical_digest(plan)
    metadata = _presentation_metadata(
        INTERACTIVE_EFFECT_SPECS[effect_kind],
        _prepared_presentation(mutable_payload),
    )

    assert metadata["action"] == expected_action
    assert expected_detail in metadata["detail"]
    assert forbidden not in "\n".join(metadata.values())
    assert metadata["confirmation_phrase"] == "EXECUTE"
    assert all(len(value) <= 2_048 for value in metadata.values())


def test_host_approval_presentation_thaws_nested_immutable_snapshot() -> None:
    """Broker-frozen mappings remain valid inputs to Host-owned approval copy."""

    frozen_payload = MappingProxyType(
        {
            "redacted_plan": MappingProxyType(
                {"plan_version": "tobkiri.shell-execute.plan.v4"}
            ),
            "plan_digest": canonical_digest({"shell": "prepared"}),
            "arguments": MappingProxyType(
                {"command": ["git", "status"], "cwd": "."}
            ),
        }
    )
    metadata = _presentation_metadata(
        INTERACTIVE_EFFECT_SPECS["shell_execute"],
        SimpleNamespace(
            normalized_payload=frozen_payload,
            request_digest=canonical_digest({"prepared": "immutable"}),
        ),
    )

    assert metadata["action"] == "Run local terminal command"
    assert 'argv: ["git", "status"]' in metadata["detail"]


def test_host_approval_presentation_fails_closed_on_an_unsealed_or_malformed_plan() -> None:
    """A display transform cannot turn a malformed future effect into approval UI."""

    with pytest.raises(InteractiveEffectUnavailable):
        _presentation_metadata(
            INTERACTIVE_EFFECT_SPECS["git_push"],
            _prepared_presentation(
                {
                    "plan": {"remote_name": "origin"},
                    "plan_digest": canonical_digest({"remote_name": "forged"}),
                }
            ),
        )


def test_commit_presentation_binds_the_message_to_the_sealed_staged_tree() -> None:
    """Same commit text with a changed staged tree must produce distinct approval copy."""

    def presentation(index_tree: str) -> Mapping[str, str]:
        plan = _git_plan(
            "git-commit",
            message="Ship the prepared change",
            expected_head_ref="refs/heads/main",
            expected_index_tree=index_tree,
        )
        return _presentation_metadata(
            INTERACTIVE_EFFECT_SPECS["git_commit"],
            _prepared_presentation(
                {
                    "plan": plan,
                    "profile_id": "profile.nondefault",
                    "workspace_id": "workspace.primary",
                }
            ),
        )

    first = presentation("c" * 40)
    second = presentation("d" * 40)
    assert "staged tree: " + "c" * 40 in first["detail"]
    assert "staged tree: " + "d" * 40 in second["detail"]
    assert first["detail"] != second["detail"]


@pytest.mark.parametrize(
    ("allow_non_fast_forward", "expected_policy", "expected_notice"),
    (
        (False, "fast-forward only: yes", "non-fast-forward updates: not permitted"),
        (
            True,
            "fast-forward only: no",
            "WARNING: non-fast-forward push may overwrite remote history.",
        ),
    ),
)
def test_push_presentation_discloses_the_sealed_force_with_lease_policy(
    allow_non_fast_forward: bool,
    expected_policy: str,
    expected_notice: str,
) -> None:
    """Approval must distinguish safe pushes from remote-history overwrites."""

    plan = {
        "remote_name": "origin",
        "remote_host": "github.com",
        "destination_ref": "refs/heads/main",
        "force_with_lease": {
            "mode": "exact-remote-oid",
            "allow_non_fast_forward": allow_non_fast_forward,
            "argument": "--force-with-lease=refs/heads/main:012345",
        },
    }
    metadata = _presentation_metadata(
        INTERACTIVE_EFFECT_SPECS["git_push"],
        _prepared_presentation(
            {"plan": plan, "plan_digest": canonical_digest(plan)}
        ),
    )

    assert expected_policy in metadata["detail"]
    assert expected_notice in metadata["detail"]


def test_terminal_presentation_defaults_cwd_and_redacts_or_bounds_display_text() -> None:
    """Valid provider defaults remain visible without leaking unbounded command data."""

    secret = "super-secret-terminal-token"
    metadata = _presentation_metadata(
        INTERACTIVE_EFFECT_SPECS["shell_execute"],
        _prepared_presentation(
            {
                "redacted_plan": {
                    "plan_version": "tobkiri.shell-execute.plan.v4",
                    "stdout": "must-not-be-presented",
                    "stderr": "must-not-be-presented",
                },
                "plan_digest": canonical_digest({"shell": "prepared"}),
                "arguments": {
                    "command": ["git", "--token", secret, "x" * 5_000],
                },
            }
        ),
    )

    assert "cwd: ." in metadata["detail"]
    assert secret not in metadata["detail"]
    assert "must-not-be-presented" not in metadata["detail"]
    assert "[truncated sha256:" in metadata["detail"]
    assert len(metadata["detail"]) <= 1_600


class _EffectPort:
    """Narrow effect-port double which records no executable payload output."""

    def __init__(self) -> None:
        self.prepare_commands: list[Any] = []
        self.queries: list[Any] = []

    @staticmethod
    def _status() -> InteractiveEffectStatus:
        return InteractiveEffectStatus(
            effect_id="pending-effect-1",
            approval_request_id="interactive-effect-1",
            state="approval_pending",
            expires_at=123_456.0,
            redacted_metadata={"confirmation_phrase": "EXECUTE"},
        )

    def prepare_interactive_effect(self, command: Any) -> InteractiveEffectStatus:
        self.prepare_commands.append(command)
        return self._status()

    def get_interactive_effect(self, query: Any) -> InteractiveEffectStatus:
        self.queries.append(query)
        return self._status()

    def resume_interactive_effect(self, query: Any) -> InteractiveEffectStatus:
        self.queries.append(query)
        return self._status()

    def cancel_interactive_effect(self, query: Any) -> InteractiveEffectStatus:
        self.queries.append(query)
        return self._status()


class _Client:
    """Authenticated nested-contract client recording the exact prepare edge."""

    def __init__(self, result: Mapping[str, Any]) -> None:
        self.result = result
        self.calls: list[tuple[str, str, Mapping[str, Any]]] = []

    def invoke(
        self,
        contract_id: str,
        operation_id: str,
        payload: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        self.calls.append((contract_id, operation_id, dict(payload)))
        return self.result


class _Invocation:
    """Minimal authenticated Host invocation used by the coordinator test."""

    def __init__(self, envelope: RequestEnvelope, client: _Client) -> None:
        self.envelope = envelope
        self.presentation_owner_principal_id = "presentation-owner-principal"
        self.presentation_owner_session_id = "presentation-owner-session"
        self._client = client

    def contract_client(self, **_kwargs: Any) -> _Client:
        return self._client


def _coordinator_context() -> RequestContext:
    """Return one Host-generated outer context for the coordinator Function."""

    activation = {"activation_id": "activation:interactive-effect"}
    return RequestContext(
        request_id="request.interactive-effect",
        trace_id="trace.interactive-effect",
        caller_principal=OpaqueAuthorityRef("caller-principal"),
        profile_id="profile-1",
        activation_id="activation:interactive-effect",
        activation_digest=canonical_digest(activation),
        plan_digest=canonical_digest({"plan": "interactive-effect"}),
        security_epoch=7,
        caller_session_id="session-caller",
        caller_domain_id="domain-caller",
        caller_boot_epoch=1,
        target_domain_id="domain-coordinator",
        target_boot_epoch=1,
        target_backend_digest=canonical_digest({"backend": "coordinator"}),
        profile_authority_digest=canonical_digest({"authority": "profile"}),
        fencing_token=3,
        handle_namespace="namespace-coordinator",
    )


def _coordinator_binding() -> Any:
    return SimpleNamespace(
        function=SimpleNamespace(
            function_id=bridge._EFFECT_FUNCTION_ID,
            implementation_digest=canonical_digest({"implementation": "effect"}),
        ),
        operation=SimpleNamespace(
            contract_id=bridge._EFFECT_CONTRACT_ID,
            contract_version="1.0.0",
            operation_id=bridge._EFFECT_OPERATION,
        ),
        principal_ref=OpaqueAuthorityRef("coordinator-principal"),
        artifact=SimpleNamespace(digest=canonical_digest({"artifact": "effect"})),
    )


def _capture_coordinator(port: _EffectPort) -> Any:
    binding = _coordinator_binding()
    return bridge.HOST_PROVIDER_FACTORY[bridge._EFFECT_FUNCTION_ID].capture(
        HostProviderCaptureContextV4(
            profile_id="profile-1",
            plan_digest=canonical_digest({"plan": "interactive-effect"}),
            security_epoch=7,
            activation={"activation_id": "activation:interactive-effect"},
            state_root=Path("/tmp/interactive-effect-coordinator"),
            provider_bindings=(binding,),
            catalog_bindings=(binding,),
            domain_ids={
                (
                    bridge._EFFECT_CONTRACT_ID,
                    bridge._EFFECT_OPERATION,
                    "coordinator-principal",
                ): "domain-coordinator"
            },
            interactive_effect_port=port,
        )
    )


def _effect_envelope() -> RequestEnvelope:
    return RequestEnvelope(
        context=_coordinator_context(),
        target_principal=OpaqueAuthorityRef("coordinator-principal"),
        target_domain=OpaqueAuthorityRef("domain-coordinator"),
        contract_id=bridge._EFFECT_CONTRACT_ID,
        contract_version="1.0.0",
        operation_id=bridge._EFFECT_OPERATION,
        payload={},
        request_digest=canonical_digest({"request": "interactive-effect"}),
        deadline_monotonic=10_000_000.0,
        lease=OpaqueInvocationLease(b"opaque-lease"),
        idempotency_key=None,
    )


def test_bridge_runs_only_signed_prepare_then_hands_a_redacted_future_to_port() -> None:
    """The coordinator never uses the legacy direct command-process runner."""

    port = _EffectPort()
    contribution = _capture_coordinator(port).contributions[0]
    prepared = {
        "redacted_plan": {"plan_version": "tobkiri.shell-execute.plan.v4"},
        "plan_digest": canonical_digest({"plan": "shell"}),
        "executed": False,
    }
    client = _Client(prepared)

    result = contribution.invoke(
        bridge._EFFECT_OPERATION,
        {
            "phase": "prepare",
            "effect_kind": "shell_execute",
            "request": {"command": ["git", "status"], "cwd": ".", "env": {}},
        },
        _Invocation(_effect_envelope(), client),
    )

    spec = INTERACTIVE_EFFECT_SPECS["shell_execute"]
    assert client.calls == [
        (
            spec.prepare_contract_id,
            spec.prepare_operation_id,
            {"command": ["git", "status"], "cwd": ".", "env": {}},
        )
    ]
    assert port.prepare_commands[0].prepared_result == prepared
    assert (
        port.prepare_commands[0].presentation_owner_principal_id
        == "presentation-owner-principal"
    )
    assert (
        port.prepare_commands[0].presentation_owner_session_id
        == "presentation-owner-session"
    )
    assert set(result) == {
        "effect_id",
        "approval_request_id",
        "state",
        "expires_at",
        "redacted_metadata",
    }
    assert isinstance(result["expires_at"], int)
    assert "command" not in result
    assert "HostBoundedProcessRunner" not in Path(bridge.__file__).read_text(
        encoding="utf-8"
    )


def test_bridge_rejects_authority_claim_before_prepare_edge_is_called() -> None:
    """A UI cannot smuggle a token or provider identity into a signed prepare."""

    port = _EffectPort()
    contribution = _capture_coordinator(port).contributions[0]
    client = _Client({})

    with pytest.raises(PermissionError, match="authority"):
        contribution.invoke(
            bridge._EFFECT_OPERATION,
            {
                "phase": "prepare",
                "effect_kind": "shell_execute",
                "request": {"token": "forged"},
            },
            _Invocation(_effect_envelope(), client),
        )

    assert client.calls == []
    assert port.prepare_commands == []


@pytest.mark.parametrize("phase", ["status", "resume", "cancel"])
def test_bridge_preserves_host_origin_owner_for_management(phase: str) -> None:
    """Nested coordinator calls retain the UI owner captured by the Host."""

    port = _EffectPort()
    contribution = _capture_coordinator(port).contributions[0]

    contribution.invoke(
        bridge._EFFECT_OPERATION,
        {"phase": phase, "effect_id": "pending-effect-1"},
        _Invocation(_effect_envelope(), _Client({})),
    )

    assert len(port.queries) == 1
    assert (
        port.queries[0].presentation_owner_principal_id
        == "presentation-owner-principal"
    )
    assert (
        port.queries[0].presentation_owner_session_id
        == "presentation-owner-session"
    )


class _PreparedBroker:
    """Broker double exposing only the Host-only prepare entrypoint."""

    def __init__(self, *, principal: OpaqueAuthorityRef) -> None:
        self.principal = principal
        self.frames: list[Any] = []

    def prepare(self, frame: Any, context: RequestContext) -> Any:
        self.frames.append((frame, context))
        return SimpleNamespace(
            binding=SimpleNamespace(
                operation=SimpleNamespace(
                    contract_id=frame.contract_id,
                    operation_id=frame.operation_id,
                ),
                principal_ref=self.principal,
            ),
            request_digest=canonical_digest(
                {"request_id": context.request_id, "payload": dict(frame.payload)}
            ),
            normalized_payload=dict(frame.payload),
        )


class _PendingController:
    """Controller double asserting presentation ownership stays Host context data."""

    def __init__(self) -> None:
        self.prepares: list[Mapping[str, Any]] = []
        self.owner_calls: list[tuple[str, str, str]] = []

    @staticmethod
    def _status() -> Any:
        return SimpleNamespace(
            effect_id="pending-effect-1",
            approval_request_id="interactive-effect-1",
            state=SimpleNamespace(value="approval_pending"),
            expires_at=123_456.0,
            presentation_metadata={"confirmation_phrase": "EXECUTE"},
        )

    def prepare(self, **kwargs: Any) -> Any:
        self.prepares.append(kwargs)
        return self._status()

    def status_for_presentation(self, **kwargs: Any) -> Any:
        self.owner_calls.append(
            (
                "status",
                kwargs["presentation_owner_principal_id"],
                kwargs["presentation_owner_session_id"],
            )
        )
        return self._status()

    def resume_for_presentation(self, **kwargs: Any) -> Any:
        self.owner_calls.append(
            (
                "resume",
                kwargs["presentation_owner_principal_id"],
                kwargs["presentation_owner_session_id"],
            )
        )
        return self._status()

    def cancel_for_presentation(self, **kwargs: Any) -> Any:
        self.owner_calls.append(
            (
                "cancel",
                kwargs["presentation_owner_principal_id"],
                kwargs["presentation_owner_session_id"],
            )
        )
        return self._status()


def _interactive_service(*, broker_principal: str = "execute-principal") -> tuple[Any, ...]:
    """Create a Host service with one signed shell execute route."""

    outer = _coordinator_context()
    inner = replace(
        outer,
        request_id="request.inner-effect",
        trace_id="trace.inner-effect",
        caller_principal=OpaqueAuthorityRef("coordinator-principal"),
        caller_session_id="session.inner-effect",
        caller_domain_id="domain-inner-effect",
        target_domain_id="domain-execute",
        target_backend_digest=canonical_digest({"backend": "execute"}),
        handle_namespace="namespace-execute",
    )
    ceiling = AuthorityScope(
        capability="effect.execute",
        semantics_digest=canonical_digest({"semantics": "shell"}),
    )
    route = CapturedInteractiveEffectRoute(
        spec=INTERACTIVE_EFFECT_SPECS["shell_execute"],
        coordinator_principal=OpaqueAuthorityRef("coordinator-principal"),
        execute_target_principal=OpaqueAuthorityRef("execute-principal"),
        execute_ceiling=ceiling,
    )
    broker = _PreparedBroker(principal=OpaqueAuthorityRef(broker_principal))
    controller = _PendingController()
    service = HostInteractiveEffectService(
        broker=broker,  # type: ignore[arg-type]
        controller=controller,  # type: ignore[arg-type]
        routes=(route,),
        context_for_execute=lambda _route, _outer: inner,
        assert_current_capture=lambda: None,
        profile_id=outer.profile_id,
        activation_id=outer.activation_id,
        plan_digest=outer.plan_digest,
        security_epoch=outer.security_epoch,
        clock=lambda: 100.0,
    )
    return service, broker, controller, outer


def test_host_service_prepares_execute_snapshot_and_scopes_all_owner_dimensions() -> None:
    """Only the execute binding reaches Broker.prepare after a fixed transform."""

    service, broker, controller, outer = _interactive_service()
    result = service.prepare_interactive_effect(
        bridge.InteractiveEffectPrepareCommand(
            context=outer,
            coordinator_principal=OpaqueAuthorityRef("coordinator-principal"),
            presentation_owner_principal_id="caller-principal",
            presentation_owner_session_id="session-caller",
            effect_kind="shell_execute",
            payload={"command": ["git", "status"], "cwd": ".", "env": {}},
            prepared_result={
                "redacted_plan": {"plan_version": "tobkiri.shell-execute.plan.v4"},
                "plan_digest": canonical_digest({"plan": "shell"}),
                "executed": False,
            },
        )
    )

    frame, context = broker.frames[0]
    assert frame.contract_id == INTERACTIVE_EFFECT_SPECS["shell_execute"].execute_contract_id
    assert frame.operation_id == INTERACTIVE_EFFECT_SPECS["shell_execute"].execute_operation_id
    assert context.caller_principal.value == "coordinator-principal"
    prepared = controller.prepares[0]
    scope = prepared["effect_scope"]
    assert scope["dimensions"]["caller_session_id"] == ["session.inner-effect"]
    assert scope["dimensions"]["plan_digest"] == [outer.plan_digest]
    assert scope["dimensions"]["invocation_owner_id"][0].startswith(
        "interactive-effect-owner."
    )
    assert prepared["presentation_owner_principal_id"] == "caller-principal"
    assert prepared["presentation_owner_session_id"] == "session-caller"
    assert prepared["presentation_metadata"] == {
        "action": "Run local terminal command",
        "summary": "Run the prepared local terminal command.",
        "detail": 'argv: ["git", "status"]\ncwd: .',
        "confirmation_phrase": "EXECUTE",
    }
    assert result.effect_id == "pending-effect-1"


def test_host_service_rejects_browser_presentation_copy_before_preparing_an_effect() -> None:
    """The coordinator accepts no browser-controlled approval presentation data."""

    service, broker, _controller, outer = _interactive_service()
    with pytest.raises(InteractiveEffectUnavailable):
        service.prepare_interactive_effect(
            bridge.InteractiveEffectPrepareCommand(
                context=outer,
                coordinator_principal=OpaqueAuthorityRef("coordinator-principal"),
                presentation_owner_principal_id="caller-principal",
                presentation_owner_session_id="session-caller",
                effect_kind="shell_execute",
                payload={
                    "command": ["git", "status"],
                    "cwd": ".",
                    "presentation": {"detail": "client-forged approval copy"},
                },
                prepared_result={
                    "redacted_plan": {
                        "plan_version": "tobkiri.shell-execute.plan.v4"
                    },
                    "plan_digest": canonical_digest({"plan": "shell"}),
                    "executed": False,
                },
            )
        )
    assert broker.frames == []


def test_host_service_owner_commands_retain_outer_principal_and_session() -> None:
    """Status, resume, and cancel never turn effect_id into authorization."""

    service, _broker, controller, outer = _interactive_service()
    query = bridge.InteractiveEffectOwnerQuery(
        context=outer,
        coordinator_principal=OpaqueAuthorityRef("coordinator-principal"),
        presentation_owner_principal_id="caller-principal",
        presentation_owner_session_id="session-caller",
        effect_id="pending-effect-1",
    )

    service.get_interactive_effect(query)
    service.resume_interactive_effect(query)
    service.cancel_interactive_effect(query)

    assert controller.owner_calls == [
        ("status", "caller-principal", "session-caller"),
        ("resume", "caller-principal", "session-caller"),
        ("cancel", "caller-principal", "session-caller"),
    ]


def test_host_service_fails_closed_on_execute_binding_or_outer_session_mismatch() -> None:
    """A forged target principal or stale panel identity cannot create an effect."""

    service, broker, _controller, outer = _interactive_service(
        broker_principal="other-execute-principal"
    )
    command = bridge.InteractiveEffectPrepareCommand(
        context=outer,
        coordinator_principal=OpaqueAuthorityRef("coordinator-principal"),
        presentation_owner_principal_id="caller-principal",
        presentation_owner_session_id="session-caller",
        effect_kind="shell_execute",
        payload={"command": ["git", "status"], "cwd": ".", "env": {}},
        prepared_result={
            "redacted_plan": {"plan_version": "tobkiri.shell-execute.plan.v4"},
            "plan_digest": canonical_digest({"plan": "shell"}),
            "executed": False,
        },
    )

    with pytest.raises(InteractiveEffectUnavailable):
        service.prepare_interactive_effect(command)
    assert len(broker.frames) == 1
    with pytest.raises(InteractiveEffectUnavailable):
        service.get_interactive_effect(
            bridge.InteractiveEffectOwnerQuery(
                context=replace(outer, caller_session_id=""),
                coordinator_principal=OpaqueAuthorityRef("coordinator-principal"),
                presentation_owner_principal_id="caller-principal",
                presentation_owner_session_id="session-caller",
                effect_id="pending-effect-1",
            )
        )
