"""Production Host Provider coverage for canonical V4 shell execution."""

from __future__ import annotations

import inspect
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping

import pytest

from core_runtime.host_provider_backend_v4 import HostProviderCaptureContextV4
from ecosystem.rumi_shell_execute_pack.runtime import execute as shell_runtime
from tobkiri_host.models import OpaqueAuthorityRef
from tobkiri_protocol.canonical import canonical_digest


_PREPARE_FUNCTION_ID = "rumi_shell_execute_pack.shell-prepare.service"
_EXECUTE_FUNCTION_ID = "rumi_shell_execute_pack.shell-execute.service"
_CONTRACT_ID = "tobkiri.service.shell.execute.v1"
_PREPARE = "rumi_shell_execute_pack.shell-prepare"
_EXECUTE = "rumi_shell_execute_pack.shell-execute"


class _Dispatch:
    """Deterministic policy and workspace provider double."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.snapshot_revision = 1
        self.mount_revision = 1
        self.policy_revision = "policy.v1"
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def invoke(
        self,
        contract_id: str,
        operation_id: str,
        payload: Mapping[str, Any],
        *,
        version_range: str | None = None,
    ) -> Mapping[str, Any]:
        assert version_range is None
        self.calls.append((contract_id, operation_id, dict(payload)))
        if (
            contract_id == "tobkiri.resource.workspace.v1"
            and operation_id == "rumi_workspace_mount_pack.workspace-resource"
            and payload.get("operation") == "list"
        ):
            return {
                "profile_id": "defaults",
                "revision": self.snapshot_revision,
                "selected_workspace_id": "workspace.fixture",
            }
        if (
            contract_id == "tobkiri.resource.workspace.v1"
            and operation_id == "rumi_workspace_mount_pack.workspace-resource"
            and payload.get("operation") == "get"
        ):
            return {
                "id": "workspace.fixture",
                "root_path": str(self.root),
                "mount_revision": self.mount_revision,
            }
        if (
            contract_id == "tobkiri.service.shell.inspect.v1"
            and operation_id == "rumi_shell_policy_pack.shell-inspect"
            and payload.get("operation") == "classify"
        ):
            return {
                "classification": self.policy_revision,
                "risk_level": "low",
                "risk_reasons": ["fixture"],
                "read_only": True,
                "approval_required": True,
                "shell_syntax": False,
                "command_hash": canonical_digest(payload.get("command")),
                "normalized_command": payload.get("command"),
            }
        raise AssertionError((contract_id, operation_id, payload))


class _Invocation:
    """Expose only the authenticated profile and requested dependencies."""

    def __init__(
        self,
        dispatch: _Dispatch,
        *,
        profile_id: str = "defaults",
        caller: str = "fixture-caller",
    ) -> None:
        self.dispatch = dispatch
        self.envelope = SimpleNamespace(
            context=SimpleNamespace(
                profile_id=profile_id,
                caller_principal=OpaqueAuthorityRef(canonical_digest({"caller": caller})),
                caller_session_id=f"session.{caller}",
                caller_domain_id=f"domain.{caller}",
                activation_id="activation.shell-host-provider",
                plan_digest=canonical_digest({"plan": "shell-host-provider"}),
                security_epoch=1,
            )
        )
        self.requests: list[tuple[frozenset[str], str]] = []

    def contract_client(
        self,
        *,
        allowed_contract_ids: frozenset[str],
        consumer_pack_id: str,
    ) -> _Dispatch:
        self.requests.append((allowed_contract_ids, consumer_pack_id))
        return self.dispatch


class _Runner:
    """Record exact bounded executions without spawning a process."""

    calls: list[dict[str, Any]] = []

    def run_local(self, **request: Any) -> Any:
        self.calls.append(request)
        return SimpleNamespace(
            exit_code=0,
            stdout="fixture output\n",
            stderr="",
            stdout_truncated=False,
            stderr_truncated=False,
            timed_out=False,
            transport_error=None,
            attestation=SimpleNamespace(
                authority="core_runtime.bounded_process_runner",
                boundary="bounded_host_process",
                sandboxed=False,
                process_tree_kill="not_required",
            ),
        )


def _binding(operation_id: str) -> Any:
    function_id = _PREPARE_FUNCTION_ID if operation_id == _PREPARE else _EXECUTE_FUNCTION_ID
    principal_id = canonical_digest({"principal": function_id})
    return SimpleNamespace(
        function=SimpleNamespace(
            function_id=function_id,
            implementation_digest=canonical_digest({"implementation": function_id}),
        ),
        operation=SimpleNamespace(
            contract_id=_CONTRACT_ID,
            contract_version="1.0.0",
            operation_id=operation_id,
        ),
        principal_ref=OpaqueAuthorityRef(principal_id),
        artifact=SimpleNamespace(digest=canonical_digest({"artifact": function_id})),
    )


def _captured() -> dict[str, Any]:
    bindings = (_binding(_PREPARE), _binding(_EXECUTE))
    contributions = []
    for index, binding in enumerate(bindings):
        key = (
            binding.operation.contract_id,
            binding.operation.operation_id,
            binding.principal_ref.value,
        )
        factory = shell_runtime.HOST_PROVIDER_FACTORY[binding.function.function_id]
        captured = factory.capture(
            HostProviderCaptureContextV4(
                profile_id="defaults",
                plan_digest=canonical_digest({"plan": "shell-host-provider"}),
                security_epoch=1,
                activation={"activation_id": "activation.shell-host-provider"},
                state_root=Path("/tmp/shell-host-provider-state"),
                provider_bindings=(binding,),
                catalog_bindings=(),
                domain_ids={key: f"domain.shell.{index}"},
            )
        )
        assert len(captured.contributions) == 1
        contributions.extend(captured.contributions)
    assert {item.operation_id for item in contributions} == {
        _PREPARE,
        _EXECUTE,
    }
    assert len({item.principal_id for item in contributions}) == 2
    return {item.operation_id: item for item in contributions}


@pytest.fixture(autouse=True)
def _bounded_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    _Runner.calls = []
    monkeypatch.setattr(shell_runtime, "HostBoundedProcessRunner", _Runner)


def _request() -> dict[str, Any]:
    return {
        "command": ["python3", "-c", "print('fixture')"],
        "cwd": ".",
        "timeout": 15,
        "env": {"LANG": "C"},
        "shell": False,
    }


def test_shell_prepare_and_execute_revalidate_then_run_once(tmp_path: Path) -> None:
    """The approved canonical plan is rebuilt before one bounded execution."""

    contributions = _captured()
    dispatch = _Dispatch(tmp_path)
    invocation = _Invocation(dispatch)

    prepared = contributions[_PREPARE].invoke(_PREPARE, _request(), invocation)
    assert prepared["executed"] is False
    redacted_plan = prepared["redacted_plan"]
    assert redacted_plan["profile_id"] == "defaults"
    assert redacted_plan["workspace"]["workspace_id"] == "workspace.fixture"
    assert redacted_plan["request"]["command_name"] == "python3"
    assert redacted_plan["request"]["argument_count"] == 3
    assert "argv" not in redacted_plan
    assert "environment" not in redacted_plan
    assert _Runner.calls == []

    result = contributions[_EXECUTE].invoke(
        _EXECUTE,
        {
            "redacted_plan": redacted_plan,
            "plan_digest": prepared["plan_digest"],
            "arguments": _request(),
        },
        invocation,
    )

    assert result["executed"] is True
    assert result["plan_digest"] == prepared["plan_digest"]
    assert "authority_receipt_redeemed" not in result
    assert len(_Runner.calls) == 1
    assert Path(_Runner.calls[0]["argv"][0]).is_absolute()
    assert _Runner.calls[0]["argv"][1:] == ["-c", "print('fixture')"]
    assert _Runner.calls[0]["environment"]["PATH"] == os.defpath
    assert _Runner.calls[0]["policy"].allowed_executables == frozenset(
        {_Runner.calls[0]["argv"][0]}
    )
    assert invocation.requests == [
        (
            frozenset(
                {
                    "tobkiri.service.shell.inspect.v1",
                    "tobkiri.resource.workspace.v1",
                }
            ),
            "rumi_shell_execute_pack",
        ),
        (
            frozenset(
                {
                    "tobkiri.service.shell.inspect.v1",
                    "tobkiri.resource.workspace.v1",
                }
            ),
            "rumi_shell_execute_pack",
        ),
    ]
    assert [
        (contract_id, operation_id, payload["operation"])
        for contract_id, operation_id, payload in dispatch.calls
    ] == [
        (
            "tobkiri.service.shell.inspect.v1",
            "rumi_shell_policy_pack.shell-inspect",
            "classify",
        ),
        (
            "tobkiri.resource.workspace.v1",
            "rumi_workspace_mount_pack.workspace-resource",
            "list",
        ),
        (
            "tobkiri.resource.workspace.v1",
            "rumi_workspace_mount_pack.workspace-resource",
            "get",
        ),
        (
            "tobkiri.service.shell.inspect.v1",
            "rumi_shell_policy_pack.shell-inspect",
            "classify",
        ),
        (
            "tobkiri.resource.workspace.v1",
            "rumi_workspace_mount_pack.workspace-resource",
            "list",
        ),
        (
            "tobkiri.resource.workspace.v1",
            "rumi_workspace_mount_pack.workspace-resource",
            "get",
        ),
    ]

    contributions_after_restart = _captured()
    result_after_restart = contributions_after_restart[_EXECUTE].invoke(
        _EXECUTE,
        {
            "redacted_plan": redacted_plan,
            "plan_digest": prepared["plan_digest"],
            "arguments": _request(),
        },
        invocation,
    )
    assert result_after_restart["executed"] is True
    assert len(_Runner.calls) == 2

    with pytest.raises(PermissionError, match="changed after prepare"):
        contributions[_EXECUTE].invoke(
            _EXECUTE,
            {
                "redacted_plan": redacted_plan,
                "plan_digest": prepared["plan_digest"],
                "arguments": {**_request(), "timeout": 16},
            },
            invocation,
        )
    assert len(_Runner.calls) == 2


def test_shell_execute_rejects_stale_workspace_before_runner(tmp_path: Path) -> None:
    """Workspace mount CAS changes invalidate the prepared plan."""

    contributions = _captured()
    dispatch = _Dispatch(tmp_path)
    invocation = _Invocation(dispatch)
    prepared = contributions[_PREPARE].invoke(_PREPARE, _request(), invocation)
    dispatch.mount_revision += 1

    with pytest.raises(PermissionError, match="changed after prepare"):
        contributions[_EXECUTE].invoke(
            _EXECUTE,
            {
                "redacted_plan": prepared["redacted_plan"],
                "plan_digest": prepared["plan_digest"],
                "arguments": _request(),
            },
            invocation,
        )
    assert _Runner.calls == []


def test_shell_execute_rejects_stale_policy_before_runner(tmp_path: Path) -> None:
    """Current policy must reproduce the exact approval-time classification."""

    contributions = _captured()
    dispatch = _Dispatch(tmp_path)
    invocation = _Invocation(dispatch)
    prepared = contributions[_PREPARE].invoke(_PREPARE, _request(), invocation)
    dispatch.policy_revision = "policy.v2"

    with pytest.raises(PermissionError, match="changed after prepare"):
        contributions[_EXECUTE].invoke(
            _EXECUTE,
            {
                "redacted_plan": prepared["redacted_plan"],
                "plan_digest": prepared["plan_digest"],
                "arguments": _request(),
            },
            invocation,
        )
    assert _Runner.calls == []


def test_shell_execute_accepts_coordinator_resume_after_prepare(tmp_path: Path) -> None:
    """Broker authority, not Provider-local caller state, owns approval resume."""

    contributions = _captured()
    dispatch = _Dispatch(tmp_path)
    prepared = contributions[_PREPARE].invoke(
        _PREPARE,
        _request(),
        _Invocation(dispatch, caller="caller-a"),
    )
    result = contributions[_EXECUTE].invoke(
        _EXECUTE,
        {
            "redacted_plan": prepared["redacted_plan"],
            "plan_digest": prepared["plan_digest"],
            "arguments": _request(),
        },
        _Invocation(dispatch, caller="coordinator"),
    )
    assert result["executed"] is True
    assert len(_Runner.calls) == 1


def test_shell_execute_rejects_tampered_plan_before_runner(
    tmp_path: Path,
) -> None:
    """A client cannot alter an approved plan while retaining its CAS digest."""

    contributions = _captured()
    dispatch = _Dispatch(tmp_path)
    invocation = _Invocation(dispatch)
    prepared = contributions[_PREPARE].invoke(_PREPARE, _request(), invocation)
    tampered = {
        **prepared["redacted_plan"],
        "request": {
            **prepared["redacted_plan"]["request"],
            "command_name": "attacker",
        },
    }

    with pytest.raises(PermissionError, match="changed after prepare"):
        contributions[_EXECUTE].invoke(
            _EXECUTE,
            {
                "redacted_plan": tampered,
                "plan_digest": prepared["plan_digest"],
                "arguments": _request(),
            },
            invocation,
        )
    assert _Runner.calls == []


def test_shell_prepare_keeps_raw_argv_and_environment_host_internal(
    tmp_path: Path,
) -> None:
    """Approval metadata exposes digests, not raw argument or environment values."""

    contribution = _captured()[_PREPARE]
    invocation = _Invocation(_Dispatch(tmp_path))
    prepared = contribution.invoke(
        _PREPARE,
        {
            **_request(),
            "command": ["python3", "-c", "raw-argument-secret"],
            "env": {"LANG": "raw-environment-secret"},
        },
        invocation,
    )
    rendered = str(prepared["redacted_plan"])
    assert "raw-argument-secret" not in rendered
    assert "raw-environment-secret" not in rendered
    assert prepared["redacted_plan"]["request"]["command_digest"].startswith("sha256:")


@pytest.mark.parametrize(
    "payload",
    [
        {**_request(), "shell": True},
        {**_request(), "command": ["/usr/bin/python3", "-c", "pass"]},
        {**_request(), "command": ["sh", "-c", "true"]},
    ],
)
def test_shell_prepare_rejects_expanded_execution_surfaces(
    tmp_path: Path,
    payload: Mapping[str, Any],
) -> None:
    """V4 accepts neither shell mode nor paths/names outside the finite allowlist."""

    contribution = _captured()[_PREPARE]
    with pytest.raises(PermissionError):
        contribution.invoke(_PREPARE, payload, _Invocation(_Dispatch(tmp_path)))
    assert _Runner.calls == []


def test_shell_prepare_rejects_user_writable_allowlisted_executable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A matching executable name is insufficient without a Host-controlled path."""

    executable_root = tmp_path / "user-bin"
    executable_root.mkdir()
    executable = executable_root / "python3"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    monkeypatch.setattr(
        shell_runtime,
        "_v4_host_search_path",
        lambda: str(executable_root),
    )

    contribution = _captured()[_PREPARE]
    with pytest.raises(PermissionError, match="user-writable"):
        contribution.invoke(_PREPARE, _request(), _Invocation(_Dispatch(tmp_path)))
    assert _Runner.calls == []


@pytest.mark.parametrize(
    "field",
    sorted(shell_runtime._V4_UNTRUSTED_AUTHORITY_FIELDS),
)
def test_shell_v4_rejects_client_authority_fields(
    tmp_path: Path,
    field: str,
) -> None:
    """Client approval-looking values never authorize the V4 Provider path."""

    contribution = _captured()[_PREPARE]
    invocation = _Invocation(_Dispatch(tmp_path))
    with pytest.raises(PermissionError, match="client shell authority field"):
        contribution.invoke(_PREPARE, {**_request(), field: "client-value"}, invocation)
    assert _Runner.calls == []


def test_shell_execute_rejects_client_approved_flag(tmp_path: Path) -> None:
    """An approved flag cannot authorize stateless Host execution."""

    contributions = _captured()
    invocation = _Invocation(_Dispatch(tmp_path))
    prepared = contributions[_PREPARE].invoke(_PREPARE, _request(), invocation)
    with pytest.raises(PermissionError, match="client shell authority field"):
        contributions[_EXECUTE].invoke(
            _EXECUTE,
            {
                "redacted_plan": prepared["redacted_plan"],
                "plan_digest": prepared["plan_digest"],
                "arguments": {**_request(), "approved": True},
            },
            invocation,
        )
    assert _Runner.calls == []


def test_shell_v4_active_path_has_no_receipt_redemption() -> None:
    """Legacy receipt compatibility is unreachable from the V4 Provider service."""

    source = inspect.getsource(shell_runtime.ShellExecuteV4Service)
    assert "_redeem" not in source
    assert shell_runtime.AUTHORITY not in shell_runtime._V4_DEPENDENCIES
    assert shell_runtime._V4_DEPENDENCIES == frozenset(
        {
            "tobkiri.service.shell.inspect.v1",
            "tobkiri.resource.workspace.v1",
        }
    )


def test_shell_factories_each_require_one_exact_operation_principal() -> None:
    """Prepare and execute cannot share or multiply one Function principal."""

    binding = _binding(_EXECUTE)
    assert set(shell_runtime.HOST_PROVIDER_FACTORY) == {
        _PREPARE_FUNCTION_ID,
        _EXECUTE_FUNCTION_ID,
    }
    prepare_factory = shell_runtime.HOST_PROVIDER_FACTORY[_PREPARE_FUNCTION_ID]
    with pytest.raises(PermissionError, match="bindings are incomplete"):
        prepare_factory.capture(
            HostProviderCaptureContextV4(
                profile_id="defaults",
                plan_digest=canonical_digest({"plan": "wrong-shell-operation"}),
                security_epoch=1,
                activation={"activation_id": "activation.wrong-shell-operation"},
                state_root=Path("/tmp/wrong-shell-operation-state"),
                provider_bindings=(binding,),
                catalog_bindings=(),
                domain_ids={},
            )
        )
    execute_factory = shell_runtime.HOST_PROVIDER_FACTORY[_EXECUTE_FUNCTION_ID]
    with pytest.raises(PermissionError, match="bindings are incomplete"):
        execute_factory.capture(
            HostProviderCaptureContextV4(
                profile_id="defaults",
                plan_digest=canonical_digest({"plan": "duplicate-shell-operation"}),
                security_epoch=1,
                activation={"activation_id": "activation.duplicate-shell-operation"},
                state_root=Path("/tmp/duplicate-shell-operation-state"),
                provider_bindings=(binding, binding),
                catalog_bindings=(),
                domain_ids={},
            )
        )
