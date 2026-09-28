"""Exact Host Provider coverage for the file inspection Pack."""

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ecosystem.rumi_file_inspect_pack.runtime import inspect
from tobkiri_protocol.canonical import canonical_digest


def _binding(operation_id: str) -> SimpleNamespace:
    principal = f"principal:{operation_id}"
    return SimpleNamespace(
        operation=SimpleNamespace(
            contract_id=inspect.CONTRACT_ID,
            contract_version=inspect.CONTRACT_VERSION,
            operation_id=operation_id,
        ),
        principal_ref=SimpleNamespace(value=principal),
        artifact=SimpleNamespace(digest="sha256:" + "a" * 64),
        function=SimpleNamespace(
            function_id=inspect.FUNCTION_ID,
            implementation_digest="sha256:" + "b" * 64,
        ),
    )


def _context(tmp_path: Path, bindings: tuple[SimpleNamespace, ...]) -> SimpleNamespace:
    return SimpleNamespace(
        profile_id="defaults",
        plan_digest="sha256:" + "c" * 64,
        security_epoch=4,
        activation={"activation_id": "activation-1"},
        state_root=tmp_path,
        provider_bindings=bindings,
        domain_ids={
            (
                binding.operation.contract_id,
                binding.operation.operation_id,
                binding.principal_ref.value,
            ): f"domain:{binding.operation.operation_id}"
            for binding in bindings
        },
    )


def _workspace_binding(root: Path) -> dict[str, object]:
    stat = root.stat()
    binding: dict[str, object] = {
        "workspace_id": "workspace-1",
        "access": "read_only",
        "mount_revision": 7,
        "canonical_root": str(root.resolve()),
        "root_st_dev": int(stat.st_dev),
        "root_st_ino": int(stat.st_ino),
    }
    binding["root_identity"] = hashlib.sha256(
        json.dumps(
            binding,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    return binding


class _WorkspaceClient:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    def invoke(
        self,
        contract_id: str,
        operation_id: str,
        payload: dict[str, object],
    ) -> dict[str, object]:
        self.calls.append((contract_id, operation_id, dict(payload)))
        assert contract_id == inspect.WORKSPACE
        assert operation_id == inspect.WORKSPACE_OPERATION
        if payload["operation"] == "list":
            return {"selected_workspace_id": "workspace-1"}
        return {
            "workspace_id": "workspace-1",
            "root_path": str(self.root),
            "mount_revision": 7,
        }


class _Invocation:
    def __init__(
        self,
        context: SimpleNamespace,
        binding: SimpleNamespace,
        payload: dict[str, object],
        client: _WorkspaceClient,
    ) -> None:
        self.client = client
        self.current_checks = 0
        self.envelope = SimpleNamespace(
            contract_id=inspect.CONTRACT_ID,
            contract_version=inspect.CONTRACT_VERSION,
            operation_id=binding.operation.operation_id,
            target_principal=binding.principal_ref,
            target_domain=SimpleNamespace(
                value=context.domain_ids[
                    (
                        inspect.CONTRACT_ID,
                        binding.operation.operation_id,
                        binding.principal_ref.value,
                    )
                ]
            ),
            payload=payload,
            context=SimpleNamespace(
                profile_id=context.profile_id,
                activation_id=context.activation["activation_id"],
                activation_digest=canonical_digest(dict(context.activation)),
                plan_digest=context.plan_digest,
                security_epoch=context.security_epoch,
            ),
        )

    def assert_current(self) -> None:
        self.current_checks += 1

    def contract_client(self, **kwargs: object) -> _WorkspaceClient:
        assert kwargs == {
            "allowed_contract_ids": frozenset({inspect.WORKSPACE}),
            "consumer_pack_id": inspect.PACK_ID,
            "include_credentials": False,
        }
        return self.client


def test_factory_captures_selected_operation_subset(tmp_path: Path) -> None:
    binding = _binding("rumi_file_inspect_pack.file-inspect")
    captured = inspect.HOST_PROVIDER_FACTORY.capture(
        _context(tmp_path, (binding,))
    )

    assert len(captured.contributions) == 1
    contribution = captured.contributions[0]
    assert contribution.operation_id == binding.operation.operation_id
    assert contribution.principal_id == binding.principal_ref.value
    captured.close()


def test_factory_dispatches_through_canonical_workspace_contract(
    tmp_path: Path,
) -> None:
    (tmp_path / "proof.txt").write_text("verified\n", encoding="utf-8")
    binding = _binding("rumi_file_inspect_pack.file-inspect.for-media")
    context = _context(tmp_path, (binding,))
    captured = inspect.HOST_PROVIDER_FACTORY.capture(context)
    client = _WorkspaceClient(tmp_path)
    payload: dict[str, object] = {
        "name": "read",
        "profile_id": "defaults",
        "workspace_id": "workspace-1",
        "path": "proof.txt",
        "require_selected": True,
        "_workspace_binding": _workspace_binding(tmp_path),
    }
    invocation = _Invocation(context, binding, payload, client)

    result = captured.contributions[0].invoke(
        binding.operation.operation_id,
        payload,
        invocation,
    )

    assert result["content"] == "verified\n"
    assert [call[2]["operation"] for call in client.calls] == [
        "list",
        "get",
        "list",
    ]
    assert invocation.current_checks >= 6


def test_factory_rejects_changed_capture_or_invocation_before_workspace_access(
    tmp_path: Path,
) -> None:
    binding = _binding("rumi_file_inspect_pack.file-inspect")
    context = _context(tmp_path, (binding,))
    bad_binding = _binding("rumi_file_inspect_pack.file-inspect")
    bad_binding.function.function_id = "attacker.file-inspect"
    with pytest.raises(PermissionError, match="incomplete"):
        inspect.HOST_PROVIDER_FACTORY.capture(
            _context(tmp_path, (bad_binding,))
        )

    captured = inspect.HOST_PROVIDER_FACTORY.capture(context)
    client = _WorkspaceClient(tmp_path)
    payload: dict[str, object] = {
        "name": "list",
        "profile_id": "another-profile",
        "workspace_id": "workspace-1",
        "_workspace_binding": _workspace_binding(tmp_path),
    }
    invocation = _Invocation(context, binding, payload, client)
    with pytest.raises(PermissionError, match="profile changed"):
        captured.contributions[0].invoke(
            binding.operation.operation_id,
            payload,
            invocation,
        )
    assert client.calls == []
