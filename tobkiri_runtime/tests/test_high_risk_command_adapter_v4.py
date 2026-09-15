"""Focused tests for the Host-only high-risk command adapter Pack."""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping

import pytest

from core_runtime.host_provider_backend_v4 import HostProviderCaptureContextV4
from ecosystem.rumi_command_protocol_pack.runtime import high_risk_adapter as adapter
from tobkiri_host.broker import RequestEnvelope
from tobkiri_host.models import OpaqueAuthorityRef, RequestContext
from tobkiri_host.ports import OpaqueInvocationLease
from tobkiri_protocol.canonical import canonical_digest


class _CoordinatorClient:
    """Capture the sole coordinator contract calls made by the adapter."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.statuses: dict[str, dict[str, Any]] = {}
        self.resume_started = threading.Event()
        self.allow_resume = threading.Event()
        self.block_resume = False

    def invoke(
        self,
        contract_id: str,
        operation_id: str,
        payload: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        assert contract_id == "tobkiri.service.interactive-effect.v1"
        assert operation_id == "interactive_effect.manage"
        request = dict(payload)
        self.calls.append((contract_id, operation_id, request))
        phase = request["phase"]
        if phase == "prepare":
            effect_id = f"effect-{len(self.statuses) + 1}"
            status = _status(effect_id, "approval_pending")
            self.statuses[effect_id] = status
            return status
        effect_id = str(request["effect_id"])
        if phase == "resume":
            self.resume_started.set()
            if self.block_resume:
                assert self.allow_resume.wait(timeout=5)
            status = _status(effect_id, "succeeded")
            self.statuses[effect_id] = status
            return status
        if phase == "cancel":
            status = _status(effect_id, "ambiguous")
            self.statuses[effect_id] = status
            return status
        if phase == "status":
            return self.statuses[effect_id]
        raise AssertionError(phase)


class _ApprovalLifecycleCoordinator(_CoordinatorClient):
    """Host-coordinator double with an opaque one-shot approval transition."""

    def __init__(self) -> None:
        super().__init__()
        self.executed_effect_ids: list[str] = []

    def approve(self, effect_id: str) -> None:
        """Record the Host-owned approval decision; no token reaches the adapter."""

        self.statuses[effect_id] = _status(effect_id, "approved")

    def invoke(
        self,
        contract_id: str,
        operation_id: str,
        payload: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        """Resume only a Host-approved effect and record one execution."""

        assert contract_id == "tobkiri.service.interactive-effect.v1"
        assert operation_id == "interactive_effect.manage"
        request = dict(payload)
        self.calls.append((contract_id, operation_id, request))
        phase = request["phase"]
        if phase == "prepare":
            effect_id = f"effect-{len(self.statuses) + 1}"
            status = _status(effect_id, "approval_pending")
            self.statuses[effect_id] = status
            return status
        effect_id = str(request["effect_id"])
        if phase == "status":
            return self.statuses[effect_id]
        if phase == "resume":
            current = self.statuses[effect_id]
            if current["state"] != "approved":
                return current
            self.executed_effect_ids.append(effect_id)
            status = _status(effect_id, "succeeded")
            self.statuses[effect_id] = status
            return status
        if phase == "cancel":
            status = _status(effect_id, "cancelled")
            self.statuses[effect_id] = status
            return status
        raise AssertionError(phase)


class _Invocation:
    """One authenticated outer envelope plus the declared coordinator client."""

    def __init__(self, client: _CoordinatorClient, envelope: RequestEnvelope) -> None:
        self._client = client
        self.envelope = envelope
        self.bindings: list[tuple[frozenset[str], str]] = []

    def contract_client(
        self,
        *,
        allowed_contract_ids: frozenset[str],
        consumer_pack_id: str,
    ) -> _CoordinatorClient:
        self.bindings.append((allowed_contract_ids, consumer_pack_id))
        return self._client


def _status(effect_id: str, state: str) -> dict[str, Any]:
    return {
        "effect_id": effect_id,
        "approval_request_id": f"approval-{effect_id}",
        "state": state,
        "expires_at": time.time() + 300,
        "redacted_metadata": {"summary": "Confirm local change"},
    }


def _binding(profile_id: str = "profile-a") -> Any:
    principal = canonical_digest({"principal": profile_id})
    return SimpleNamespace(
        function=SimpleNamespace(
            function_id="rumi_command_protocol_pack.high-risk-command.service",
            implementation_digest=canonical_digest({"implementation": profile_id}),
        ),
        operation=SimpleNamespace(
            contract_id="tobkiri.service.command.high-risk.v1",
            contract_version="1.0.0",
            operation_id="high_risk_command.manage",
        ),
        principal_ref=OpaqueAuthorityRef(principal),
        artifact=SimpleNamespace(digest=canonical_digest({"artifact": profile_id})),
    )


def _capture(state_root: Path, *, profile_id: str = "profile-a") -> Any:
    binding = _binding(profile_id)
    key = (
        binding.operation.contract_id,
        binding.operation.operation_id,
        binding.principal_ref.value,
    )
    return adapter.HOST_PROVIDER_FACTORY.capture(
        HostProviderCaptureContextV4(
            profile_id=profile_id,
            plan_digest=canonical_digest({"plan": profile_id}),
            security_epoch=9,
            activation={"activation_id": f"activation-{profile_id}"},
            state_root=state_root,
            provider_bindings=(binding,),
            catalog_bindings=(binding,),
            domain_ids={key: f"domain.{profile_id}"},
        )
    )


def _envelope(
    *,
    profile_id: str = "profile-a",
    caller: str = "caller-a",
    session: str = "session-a",
) -> RequestEnvelope:
    binding = _binding(profile_id)
    activation = {"activation_id": f"activation-{profile_id}"}
    context = RequestContext(
        request_id="outer-request",
        trace_id="trace-command-adapter",
        caller_principal=OpaqueAuthorityRef(caller),
        profile_id=profile_id,
        activation_id=f"activation-{profile_id}",
        activation_digest=canonical_digest(activation),
        plan_digest=canonical_digest({"plan": profile_id}),
        security_epoch=9,
        caller_session_id=session,
        caller_domain_id="domain.caller",
        caller_boot_epoch=1,
        target_domain_id=f"domain.{profile_id}",
        target_boot_epoch=1,
        target_backend_digest=canonical_digest({"backend": profile_id}),
        profile_authority_digest=canonical_digest({"authority": profile_id}),
        fencing_token=1,
        handle_namespace="command-adapter",
    )
    return RequestEnvelope(
        context=context,
        target_principal=binding.principal_ref,
        target_domain=OpaqueAuthorityRef(f"domain.{profile_id}"),
        contract_id="tobkiri.service.command.high-risk.v1",
        contract_version="1.0.0",
        operation_id="high_risk_command.manage",
        payload={},
        request_digest=canonical_digest({"outer": profile_id, "caller": caller}),
        deadline_monotonic=time.monotonic() + 60,
        lease=OpaqueInvocationLease(b"adapter-lease"),
        idempotency_key=None,
    )


def _provider(state_root: Path, client: _CoordinatorClient, **envelope: Any) -> tuple[Any, _Invocation, Any]:
    captured = _capture(state_root, profile_id=envelope.get("profile_id", "profile-a"))
    contribution = captured.contributions[0]
    invocation = _Invocation(client, _envelope(**envelope))
    return contribution, invocation, captured


def _prepare_payload(invocation_id: str = "invoke-1") -> dict[str, Any]:
    return {
        "phase": "prepare",
        "invocation_id": invocation_id,
        "command_ref": "terminal",
        "arguments": {"command": "pytest -q", "cwd": "."},
        "presentation": {"summary": "Run focused tests"},
    }


def _invoke(contribution: Any, invocation: _Invocation, payload: Mapping[str, Any]) -> Mapping[str, Any]:
    return contribution.invoke("high_risk_command.manage", payload, invocation)


def test_prepare_is_strict_and_rejects_client_authority_material(tmp_path: Path) -> None:
    client = _CoordinatorClient()
    contribution, invocation, captured = _provider(tmp_path, client)
    invalid = _prepare_payload()
    invalid["effect_id"] = "client-controlled"
    with pytest.raises(adapter.HighRiskCommandUnavailable):
        _invoke(contribution, invocation, invalid)
    nested = _prepare_payload("invoke-2")
    nested["arguments"] = {"command": "pytest", "scope": "all"}
    with pytest.raises(adapter.HighRiskCommandUnavailable):
        _invoke(contribution, invocation, nested)
    profile_smuggling = _prepare_payload("invoke-profile")
    profile_smuggling["arguments"] = {
        "command": "pytest",
        "workspace_id": "workspace-allowed",
        "nested": {"profile_id": "attacker-profile"},
    }
    with pytest.raises(adapter.HighRiskCommandUnavailable):
        _invoke(contribution, invocation, profile_smuggling)
    malformed = _prepare_payload("invoke-3")
    malformed["presentation"] = {"summary": "one", "extra": "two"}
    with pytest.raises(adapter.HighRiskCommandUnavailable):
        _invoke(contribution, invocation, malformed)
    assert client.calls == []
    captured.close()


def test_factory_and_close_fence_the_exact_single_host_operation(tmp_path: Path) -> None:
    binding = _binding()
    bad_binding = _binding()
    bad_binding.function.function_id = "rumi_command_protocol_pack.sibling.service"
    key = (
        binding.operation.contract_id,
        binding.operation.operation_id,
        binding.principal_ref.value,
    )
    with pytest.raises(adapter.HighRiskCommandUnavailable):
        adapter.HOST_PROVIDER_FACTORY.capture(
            HostProviderCaptureContextV4(
                profile_id="profile-a",
                plan_digest=canonical_digest({"plan": "profile-a"}),
                security_epoch=9,
                activation={"activation_id": "activation-profile-a"},
                state_root=tmp_path,
                provider_bindings=(bad_binding,),
                catalog_bindings=(bad_binding,),
                domain_ids={key: "domain.profile-a"},
            )
        )
    client = _CoordinatorClient()
    contribution, invocation, captured = _provider(tmp_path, client)
    captured.close()
    with pytest.raises(adapter.HighRiskCommandUnavailable):
        _invoke(contribution, invocation, _prepare_payload())


def test_owner_session_and_profile_are_part_of_the_durable_lookup(tmp_path: Path) -> None:
    client = _CoordinatorClient()
    contribution, invocation, captured = _provider(tmp_path, client)
    result = _invoke(contribution, invocation, _prepare_payload())
    assert result["approval_request_id"] == "approval-effect-1"
    assert "effect_id" not in result
    other_session = _Invocation(client, _envelope(session="session-b"))
    with pytest.raises(adapter.HighRiskCommandUnavailable):
        _invoke(
            contribution,
            other_session,
            {"phase": "status", "invocation_id": "invoke-1"},
        )
    other_principal = _Invocation(client, _envelope(caller="caller-b"))
    with pytest.raises(adapter.HighRiskCommandUnavailable):
        _invoke(
            contribution,
            other_principal,
            {"phase": "status", "invocation_id": "invoke-1"},
        )
    other_contribution, other_profile, other_captured = _provider(
        tmp_path,
        client,
        profile_id="profile-b",
    )
    with pytest.raises(adapter.HighRiskCommandUnavailable):
        _invoke(
            other_contribution,
            other_profile,
            {"phase": "status", "invocation_id": "invoke-1"},
        )
    captured.close()
    other_captured.close()


def test_prepare_is_exactly_once_and_stores_no_raw_arguments(tmp_path: Path) -> None:
    client = _CoordinatorClient()
    contribution, invocation, captured = _provider(tmp_path, client)
    payload = _prepare_payload()
    payload["arguments"] = {
        "command": "pytest -q",
        "credential_handle": "credential:opaque",
        "secret_text": "must-not-be-persisted",
    }
    first = _invoke(contribution, invocation, payload)
    second = _invoke(contribution, invocation, payload)
    assert first == second
    assert [call[2]["phase"] for call in client.calls] == ["prepare"]
    assert invocation.bindings == [
        (frozenset({"tobkiri.service.interactive-effect.v1"}), "rumi_command_protocol_pack")
    ]
    database = sqlite3.connect(tmp_path / "high-risk-command-v4.sqlite3")
    try:
        persisted = database.execute(
            "SELECT safe_result_json FROM command_invocations"
        ).fetchone()[0]
    finally:
        database.close()
    assert "must-not-be-persisted" not in persisted
    assert "credential:opaque" not in persisted
    assert "effect-1" in persisted
    assert "effect_id" not in first
    captured.close()


@pytest.mark.parametrize(
    ("command_ref", "effect_kind"),
    (
        ("terminal", "shell_execute"),
        ("commit", "git_commit"),
        ("push", "git_push"),
        ("patch", "git_apply_patch"),
        ("restore", "git_restore"),
    ),
)
def test_each_high_risk_command_prepares_only_through_the_host_coordinator(
    tmp_path: Path,
    command_ref: str,
    effect_kind: str,
) -> None:
    """All five commands enter the one signed approval coordinator path."""

    client = _CoordinatorClient()
    contribution, invocation, captured = _provider(tmp_path, client)
    payload = _prepare_payload(f"invoke-{command_ref}")
    payload["command_ref"] = command_ref
    payload["presentation"] = {
        "title": "Client-forged approval title",
        "summary": "client-presentation-must-not-reach-host-approval",
    }

    result = _invoke(contribution, invocation, payload)

    assert result["state"] == "approval_pending"
    assert isinstance(result["expires_at"], int)
    assert client.calls == [
        (
            "tobkiri.service.interactive-effect.v1",
            "interactive_effect.manage",
            {
                "phase": "prepare",
                "effect_kind": effect_kind,
                "request": payload["arguments"],
            },
        )
    ]
    assert "presentation" not in client.calls[0][2]
    assert "client-presentation-must-not-reach-host-approval" not in str(
        client.calls[0][2]
    )
    captured.close()


@pytest.mark.parametrize(
    ("command_ref", "effect_kind"),
    (
        ("terminal", "shell_execute"),
        ("commit", "git_commit"),
        ("push", "git_push"),
        ("patch", "git_apply_patch"),
        ("restore", "git_restore"),
    ),
)
def test_each_high_risk_command_requires_host_approval_and_executes_once(
    tmp_path: Path,
    command_ref: str,
    effect_kind: str,
) -> None:
    """A client cannot approve, replay, or execute any of the five commands."""

    client = _ApprovalLifecycleCoordinator()
    contribution, invocation, captured = _provider(tmp_path, client)
    invocation_id = f"lifecycle-{command_ref}"
    payload = _prepare_payload(invocation_id)
    payload["command_ref"] = command_ref

    pending = _invoke(contribution, invocation, payload)
    assert pending["state"] == "approval_pending"
    assert client.calls[-1][2]["effect_kind"] == effect_kind

    unapproved = _invoke(
        contribution,
        invocation,
        {"phase": "resume", "invocation_id": invocation_id},
    )
    assert unapproved["state"] == "approval_pending"
    assert client.executed_effect_ids == []

    client.approve("effect-1")
    approved = _invoke(
        contribution,
        invocation,
        {"phase": "resume", "invocation_id": invocation_id},
    )
    assert approved["state"] == "succeeded"
    assert client.executed_effect_ids == ["effect-1"]

    replay = _invoke(
        contribution,
        invocation,
        {"phase": "resume", "invocation_id": invocation_id},
    )
    assert replay["state"] == "succeeded"
    assert client.executed_effect_ids == ["effect-1"]
    assert len([call for call in client.calls if call[2]["phase"] == "resume"]) == 2

    with pytest.raises(adapter.HighRiskCommandUnavailable):
        _invoke(
            contribution,
            invocation,
            {
                "phase": "resume",
                "invocation_id": invocation_id,
                "receipt": "client-replayed-receipt",
            },
        )
    captured.close()


@pytest.mark.parametrize(
    "command_ref", ("terminal", "commit", "push", "patch", "restore")
)
def test_restart_resyncs_a_persisted_resume_marker_without_exposing_effect_id(
    tmp_path: Path,
    command_ref: str,
) -> None:
    """Restart recovery is the same opaque invocation protocol for all five."""

    client = _CoordinatorClient()
    contribution, invocation, captured = _provider(tmp_path, client)
    invocation_id = f"restart-{command_ref}"
    payload = _prepare_payload(invocation_id)
    payload["command_ref"] = command_ref
    _invoke(contribution, invocation, payload)
    stored = contribution.invoke.__self__._store.load(  # type: ignore[attr-defined]
        adapter._Owner("caller-a", "session-a", "profile-a"),
        invocation_id,
    )
    contribution.invoke.__self__._store.replace_result(  # type: ignore[attr-defined]
        stored,
        adapter._with_state(stored.result, "resuming"),
    )
    captured.close()
    restarted, restart_invocation, restarted_capture = _provider(tmp_path, client)
    client.statuses["effect-1"] = _status("effect-1", "dispatched")
    result = _invoke(
        restarted,
        restart_invocation,
        {"phase": "status", "invocation_id": invocation_id},
    )
    assert result["state"] == "dispatched"
    assert "effect_id" not in result
    assert client.calls[-1][2] == {"phase": "status", "effect_id": "effect-1"}
    restarted_capture.close()


def test_resume_uses_host_stored_effect_id_and_single_flight(tmp_path: Path) -> None:
    client = _CoordinatorClient()
    contribution, invocation, captured = _provider(tmp_path, client)
    _invoke(contribution, invocation, _prepare_payload())
    client.block_resume = True
    outcomes: list[object] = []

    def resume() -> None:
        try:
            outcomes.append(
                _invoke(
                    contribution,
                    invocation,
                    {"phase": "resume", "invocation_id": "invoke-1"},
                )
            )
        except Exception as exc:  # pragma: no cover - asserted below
            outcomes.append(exc)

    first = threading.Thread(target=resume)
    second = threading.Thread(target=resume)
    first.start()
    assert client.resume_started.wait(timeout=5)
    status_while_busy = _invoke(
        contribution,
        invocation,
        {"phase": "status", "invocation_id": "invoke-1"},
    )
    assert status_while_busy["state"] == "resuming"
    assert len([call for call in client.calls if call[2]["phase"] == "status"]) == 0
    second.start()
    second.join(timeout=5)
    client.allow_resume.set()
    first.join(timeout=5)
    assert len([call for call in client.calls if call[2]["phase"] == "resume"]) == 1
    assert any(isinstance(item, adapter.HighRiskCommandBusy) for item in outcomes)
    result = next(item for item in outcomes if isinstance(item, Mapping))
    assert result["state"] == "succeeded"
    assert "effect_id" not in result
    with pytest.raises(adapter.HighRiskCommandUnavailable):
        _invoke(
            contribution,
            invocation,
            {"phase": "resume", "invocation_id": "invoke-1", "effect_id": "effect-2"},
        )
    captured.close()


def test_cancel_projects_coordinator_ambiguous_not_local_cancelled(tmp_path: Path) -> None:
    client = _CoordinatorClient()
    contribution, invocation, captured = _provider(tmp_path, client)
    _invoke(contribution, invocation, _prepare_payload())
    cancelled = _invoke(
        contribution,
        invocation,
        {"phase": "cancel", "invocation_id": "invoke-1"},
    )
    again = _invoke(
        contribution,
        invocation,
        {"phase": "cancel", "invocation_id": "invoke-1"},
    )
    assert cancelled["state"] == "ambiguous"
    assert again["state"] == "ambiguous"
    assert len([call for call in client.calls if call[2]["phase"] == "cancel"]) == 1
    assert "effect_id" not in cancelled
    captured.close()


def test_list_pending_excludes_terminal_rows_and_prunes_only_safe_terminal_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(adapter, "_MAX_STORED_ROWS", 1)
    client = _CoordinatorClient()
    contribution, invocation, captured = _provider(tmp_path, client)
    _invoke(contribution, invocation, _prepare_payload("invoke-resolved"))
    client.statuses["effect-1"] = _status("effect-1", "cancelled")
    resolved = _invoke(
        contribution,
        invocation,
        {"phase": "status", "invocation_id": "invoke-resolved"},
    )
    assert resolved["state"] == "cancelled"
    assert _invoke(contribution, invocation, {"phase": "list_pending"}) == {
        "invocations": []
    }
    replacement = _invoke(contribution, invocation, _prepare_payload("invoke-next"))
    assert replacement["state"] == "approval_pending"
    database = sqlite3.connect(tmp_path / "high-risk-command-v4.sqlite3")
    try:
        rows = database.execute(
            "SELECT invocation_id FROM command_invocations ORDER BY invocation_id"
        ).fetchall()
    finally:
        database.close()
    assert rows == [("invoke-next",)]
    captured.close()


def test_adapter_has_no_legacy_runner_or_direct_effect_target_imports() -> None:
    source = Path(adapter.__file__).read_text(encoding="utf-8")
    assert "HostBoundedProcessRunner" not in source
    assert "CommandOperationRegistry" not in source
    assert "tobkiri.service.shell.execute.v1" not in source
    assert "tobkiri.service.git.write.v1" not in source
    assert "tobkiri.service.git.publish.v1" not in source
    assert "tobkiri.service.interactive-effect.v1" in source
