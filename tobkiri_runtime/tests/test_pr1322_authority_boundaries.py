from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping

import pytest

from ecosystem.rumi_default_tools_pack.domain.tool import host_contract_adapter as adapter
from core_runtime.bootstrap import default_builtin_grants
from core_runtime.bounded_process_runner import (
    BoundedProcessResult,
    HostProcessAttestation,
)
from ecosystem.rumi_host_authority_bridge_pack.runtime import bridge
from ecosystem.rumi_shell_execute_pack.runtime import execute
from tobkiri_host.broker import RequestEnvelope
from tobkiri_host.models import OpaqueAuthorityRef, RequestContext
from tobkiri_host.ports import OpaqueInvocationLease


def _host_context(
    *,
    profile_id: str = "host-profile",
    workspace_id: str = "trusted-workspace",
) -> SimpleNamespace:
    """Create a valid Host envelope plus Host-owned test metadata."""

    return SimpleNamespace(
        envelope=RequestEnvelope(
            context=RequestContext(
                request_id=f"request-{profile_id}",
                trace_id=f"trace-{profile_id}",
                caller_principal=OpaqueAuthorityRef("host-caller"),
                profile_id=profile_id,
                activation_id=f"activation-{profile_id}",
                activation_digest="sha256:" + "a" * 64,
                plan_digest="sha256:" + "b" * 64,
                security_epoch=3,
                caller_session_id=f"session-{profile_id}",
                caller_domain_id="host-caller-domain",
                caller_boot_epoch=2,
                target_domain_id="host-target-domain",
                target_boot_epoch=4,
                target_backend_digest="sha256:" + "c" * 64,
                profile_authority_digest="sha256:" + "d" * 64,
                fencing_token=7,
                handle_namespace="host-handles",
            ),
            target_principal=OpaqueAuthorityRef("host-target"),
            target_domain=OpaqueAuthorityRef("host-target-domain"),
            contract_id="host.contract.v1",
            contract_version="1.0.0",
            operation_id="host.operation",
            payload={},
            request_digest="sha256:" + "e" * 64,
            deadline_monotonic=time.monotonic() + 300,
            lease=OpaqueInvocationLease(b"host-lease"),
            idempotency_key=None,
        ),
        caller_pack_id="host-caller-pack",
        caller_function_id="host-caller-function",
        profile_revision=f"revision-{profile_id}",
        workspace_id=workspace_id,
    )


def test_authority_requires_host_envelope_and_ignores_payload_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bridge, "_RECEIPT_ROOT", tmp_path / "receipts")
    payload = {
        "service_pack_id": "service-pack",
        "operation": "effect.write",
        "authority": "effect.write",
        "caller_id": "payload-caller",
        "caller_pack_id": "payload-pack",
        "caller_function_id": "payload-function",
        "profile_id": "payload-profile",
        "workspace_id": "payload-workspace",
        "session_id": "payload-session",
        "_contract_consumer_pack_id": "payload-consumer",
        "arguments": {"path": "safe.txt"},
    }

    with pytest.raises(PermissionError, match="envelope"):
        bridge._authorize(payload)

    host_context = _host_context()
    issued = bridge._authorize(payload, host_context=host_context)
    scope = issued["scope"]
    assert scope["caller_id"] == "host-caller"
    assert scope["caller_pack_id"] == "host-caller-pack"
    assert scope["caller_function_id"] == "host-caller-function"
    assert scope["profile_id"] == "host-profile"
    assert scope["profile_revision"] == "revision-host-profile"
    assert scope["activation_id"] == "activation-host-profile"
    assert scope["plan_digest"] == "sha256:" + "b" * 64
    assert scope["workspace_id"] == "trusted-workspace"

    redeemed = bridge._redeem(
        {
            **payload,
            "receipt": issued["receipt"],
            "caller_id": "different-payload-caller",
            "profile_id": "different-payload-profile",
            "workspace_id": "different-payload-workspace",
        },
        host_context=host_context,
    )
    assert redeemed["authorized"] is True


def test_authority_receipt_cannot_cross_profile_activation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bridge, "_RECEIPT_ROOT", tmp_path / "receipts")
    payload = {
        "service_pack_id": "service-pack",
        "operation": "effect.write",
        "authority": "effect.write",
        "arguments": {"path": "safe.txt"},
    }
    issued = bridge._authorize(payload, host_context=_host_context())
    mixed = bridge._redeem(
        {**payload, "receipt": issued["receipt"]},
        host_context=_host_context(profile_id="other-profile"),
    )
    assert mixed["authorized"] is False
    assert mixed["reason"] == "receipt_scope_mismatch"


def test_default_builtin_grants_are_not_implicitly_applied() -> None:
    class GrantRecorder:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, Mapping[str, Any]]] = []

        def grant_permission(
            self,
            principal_id: str,
            permission_id: str,
            config: Mapping[str, Any],
        ) -> None:
            self.calls.append((principal_id, permission_id, config))

    recorder = GrantRecorder()
    assert default_builtin_grants.DEFAULT_BUILTIN_GRANTS == ()
    assert default_builtin_grants.default_builtin_grants_enabled() is False
    assert default_builtin_grants.apply_default_builtin_grants(recorder) == []
    assert recorder.calls == []


def test_defaultspack_adapter_does_not_inject_hidden_consumer_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str, dict[str, Any]]] = []

    class CapturedSession:
        profile_id = "captured-profile"
        plan_digest = "sha256:" + "f" * 64

        def invoke(
            self,
            contract_id: str,
            operation_id: str,
            payload: Mapping[str, Any],
            **_: Any,
        ) -> dict[str, Any]:
            calls.append((contract_id, operation_id, dict(payload)))
            return {"status": "ok"}

        def provider_metadata(self, contract_id: str) -> tuple[Mapping[str, Any], ...]:
            return ({"contract_id": contract_id},)

    class Container:
        def get_or_none(self, name: str) -> CapturedSession | None:
            return CapturedSession() if name == "v4_dispatch_session" else None

    monkeypatch.setattr(adapter, "get_container", lambda: Container())
    result = adapter.run_host_contract_action(
        "browser.open_url",
        {
            "url": "https://example.test",
            "profile_id": "browser-profile-resource",
            "_contract_consumer_pack_id": "spoofed-pack",
            "_contract_consumer_function_id": "spoofed-function",
            "approved": True,
        },
        source_function_id="defaultspack.browser.open_url",
    )

    assert result == {"status": "ok"}
    assert len(calls) == 1
    request = calls[0][2]
    assert request["profile_id"] == "browser-profile-resource"
    assert "_contract_consumer_pack_id" not in request
    assert "_contract_consumer_function_id" not in request
    assert "approved" not in request


def test_shell_requires_host_context_and_uses_empty_by_default_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Client:
        def invoke(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            raise AssertionError("Host context must be checked first")

    monkeypatch.setenv("RUMI_LEAK", "must-not-reach-child")
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-reach-child")
    assert execute._environment({}) == {}
    assert "RUMI_LEAK" not in execute._host_environment({})
    assert "OPENAI_API_KEY" not in execute._host_environment({})
    with pytest.raises(PermissionError):
        execute._environment({"RUMI_LEAK": "spoof"})
    with pytest.raises(PermissionError):
        execute._environment({"API_TOKEN": "secret"})
    with pytest.raises(PermissionError, match="envelope"):
        execute.ShellExecuteService(Client()).invoke(
            "execute",
            {"command": "echo safe", "authority_receipt": "opaque"},
        )


def test_shell_passes_host_workspace_and_exact_request_to_bounded_runner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host_context = _host_context()

    class Client:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, dict[str, Any]]] = []

        def invoke(
            self,
            contract_id: str,
            operation: str,
            payload: Mapping[str, Any],
        ) -> dict[str, Any]:
            self.calls.append((contract_id, operation, dict(payload)))
            if contract_id == execute.POLICY:
                return {
                    "classification": "low",
                    "risk_reasons": ["read_only"],
                    "approval_required": False,
                    "shell_syntax": False,
                }
            if contract_id == execute.WORKSPACE:
                assert payload == {"workspace_id": "trusted-workspace"}
                return {"root_path": str(tmp_path)}
            if contract_id == execute.AUTHORITY:
                assert set(payload) == {
                    "receipt",
                    "service_pack_id",
                    "operation",
                    "authority",
                    "arguments",
                }
                assert "caller_id" not in payload
                assert "profile_id" not in payload
                assert "workspace_id" not in payload
                return {"authorized": True}
            raise AssertionError(contract_id)

    class Runner:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        def run_local(self, **kwargs: Any) -> BoundedProcessResult:
            self.calls.append(kwargs)
            return BoundedProcessResult(
                exit_code=0,
                stdout="safe\n",
                stderr="",
                timed_out=False,
                stdout_truncated=False,
                stderr_truncated=False,
                attestation=HostProcessAttestation(
                    authority="core_runtime.bounded_process_runner",
                    boundary="bounded_host_process",
                    sandboxed=False,
                    process_tree_kill="posix_process_group",
                ),
            )

    client = Client()
    runner = Runner()
    monkeypatch.setattr(execute, "HostBoundedProcessRunner", lambda: runner)
    service = execute.ShellExecuteService(
        client,
        host_context=host_context,
    )
    result = service.invoke(
        "execute",
        {
            "command": ["/bin/echo", "safe"],
            "cwd": ".",
            "authority_receipt": "opaque-receipt",
            "profile_id": "payload-profile",
            "workspace_id": "payload-workspace",
            "caller_id": "payload-caller",
            "env": {"LANG": "C"},
        },
    )

    assert result["workspace_id"] == "trusted-workspace"
    assert result["executed"] is True
    assert len(runner.calls) == 1
    call = runner.calls[0]
    assert call["environment"]["LANG"] == "C"
    assert "RUMI_LEAK" not in call["environment"]
    assert "OPENAI_API_KEY" not in call["environment"]
    assert call["policy"].allow_path_search is False
    assert call["policy"].allowed_argv == (tuple(call["argv"]),)


def test_shell_source_has_no_pack_owned_process_spawn() -> None:
    source = Path(execute.__file__).read_text(encoding="utf-8")
    assert "subprocess" not in source
    assert "Popen" not in source
