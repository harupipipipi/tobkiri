from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping

import pytest
from jsonschema import Draft202012Validator

from ecosystem.rumi_git_publish_pack.runtime import publish


def _git(repository: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(repository), *args],
        text=True,
    ).strip()


def _repository(root: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main", root], check=True)
    subprocess.run(["git", "-C", root, "config", "user.name", "Tobkiri"], check=True)
    subprocess.run(
        ["git", "-C", root, "config", "user.email", "test@example.invalid"],
        check=True,
    )
    (root / "tracked.txt").write_text("published\n", encoding="utf-8")
    subprocess.run(["git", "-C", root, "add", "tracked.txt"], check=True)
    subprocess.run(["git", "-C", root, "commit", "-q", "-m", "initial"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            root,
            "remote",
            "add",
            "origin",
            "https://example.test/org/repository.git",
        ],
        check=True,
    )


class _WorkspaceClient:
    def __init__(
        self,
        root: Path,
        *,
        credential_identity: Mapping[str, Any] | None = None,
    ) -> None:
        self.root = root
        self.revision = 1
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.credential_identity = (
            dict(credential_identity) if credential_identity is not None else None
        )
        self.credential_selections: list[dict[str, str]] = []
        self.credential_pushes: list[dict[str, Any]] = []
        self.selection_sequence = 0

    def invoke(
        self,
        contract_id: str,
        operation_id: str,
        payload: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        self.calls.append((contract_id, operation_id, dict(payload)))
        assert publish.WORKSPACE == "tobkiri.resource.workspace.v1"
        assert contract_id == "tobkiri.resource.workspace.v1"
        assert operation_id == publish.WORKSPACE_GET_OPERATION
        assert payload["operation"] == "get"
        return {
            "workspace_id": payload["workspace_id"],
            "root_path": str(self.root),
            "mount_revision": self.revision,
        }

    def push_git_https_with_credential(self, **payload: str) -> str:
        """Capture the sealed Host credential-port call without a network push."""

        self.credential_pushes.append(dict(payload))
        return "published by Host credential port"

    def select_git_https_credential(self, **payload: str) -> Mapping[str, Any] | None:
        """Return only the configured Host-owned opaque credential identity."""

        self.credential_selections.append(dict(payload))
        if self.credential_identity is None:
            return None
        self.selection_sequence += 1
        return {
            **self.credential_identity,
            "selection_receipt": (
                f"credential-selection:test-{self.selection_sequence}"
            ),
        }


def _request_context(**overrides: Any) -> SimpleNamespace:
    values = {
        "profile_id": "profile-a",
        "activation_id": "activation-a",
        "activation_digest": "sha256:" + "a" * 64,
        "plan_digest": "sha256:" + "b" * 64,
        "security_epoch": 7,
        "caller_principal": SimpleNamespace(value="sha256:" + "c" * 64),
        "caller_session_id": "session-a",
        "caller_domain_id": "domain:caller.test",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class _Invocation:
    def __init__(self, client: _WorkspaceClient, context: SimpleNamespace) -> None:
        self.client = client
        self.envelope = SimpleNamespace(context=context)
        self.bindings: list[tuple[frozenset[str], str]] = []

    def contract_client(
        self,
        *,
        allowed_contract_ids: frozenset[str],
        consumer_pack_id: str,
    ) -> _WorkspaceClient:
        self.bindings.append((allowed_contract_ids, consumer_pack_id))
        return self.client


def _service(
    root: Path,
    *,
    credential_identity: Mapping[str, Any] | None = None,
) -> tuple[publish.GitPushProviderV4, _Invocation]:
    context = _request_context()
    state_root = root / ".host-state"
    state_root.mkdir(exist_ok=True)
    capture = SimpleNamespace(
        profile_id=context.profile_id,
        plan_digest=context.plan_digest,
        security_epoch=context.security_epoch,
        state_root=state_root,
    )
    client = _WorkspaceClient(root, credential_identity=credential_identity)
    return publish.GitPushProviderV4(capture), _Invocation(client, context)


def _credential_identity(
    *,
    handle: str = "credential:opaque-git-push",
    workspace_id: str = "workspace-a",
    origin: str = "https://example.test",
) -> dict[str, Any]:
    identity = {
        "handle": handle,
        "key_version": "credential-broker.v1",
        "consumer_pack_id": publish.SERVICE_PACK_ID,
        "provider_instance_id": publish.GIT_CREDENTIAL_PROVIDER_INSTANCE_ID,
        "profile_id": "profile-a",
        "scope": publish.GIT_CREDENTIAL_SCOPE,
        "purpose": "provider.invoke",
        "resource_binding": {
            "endpoint_origin": origin,
            "workspace_id": workspace_id,
        },
    }
    identity["binding_digest"] = publish.canonical_digest(identity)
    return identity


def test_v4_prepare_binds_canonical_exact_push_plan_and_executes_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _repository(tmp_path)
    service, invocation = _service(tmp_path)
    prepared = service.invoke(
        publish.PREPARE_OPERATION,
        {
            "workspace_id": "workspace-a",
            "remote": "origin",
            "branch": "main",
            "force_with_lease": False,
        },
        invocation,
    )
    assert invocation.bindings == [
        (frozenset({"tobkiri.resource.workspace.v1"}), publish.SERVICE_PACK_ID)
    ]
    source_oid = _git(tmp_path, "rev-parse", "main")
    plan = prepared["plan"]
    assert "prepare_nonce" not in plan
    assert not hasattr(service, "_plans")
    assert plan["source_oid"] == source_oid
    assert plan["remote_oid"] == "0" * 40
    assert plan["push_url"] == "https://example.test/org/repository.git"
    assert plan["refspec"] == f"{source_oid}:refs/heads/main"
    assert plan["force_with_lease"] == {
        "mode": "exact-remote-oid",
        "allow_non_fast_forward": False,
        "argument": "--force-with-lease=refs/heads/main:" + "0" * 40,
    }
    assert prepared["plan_digest"] == publish.canonical_digest(plan)
    assert invocation.client.credential_selections == [
        {
            "workspace_id": "workspace-a",
            "endpoint_origin": "https://example.test",
            "provider_instance_id": "git-publish.service",
            "credential_scope": "git.publish",
        }
    ]

    calls: list[tuple[Path, list[str], dict[str, Any]]] = []
    real_git = publish._git
    real_revalidate = publish._revalidate_plan
    revalidations = 0

    def capture_revalidation(*args: Any, **kwargs: Any) -> None:
        nonlocal revalidations
        revalidations += 1
        real_revalidate(*args, **kwargs)

    def capture_push(
        repository: Path,
        args: list[str],
        **kwargs: Any,
    ) -> str:
        if args[:1] == ["push"]:
            assert repository.name == "transport.git"
            assert "remote.origin.url" not in _git(
                repository, "config", "--local", "--list"
            )
            calls.append((repository, list(args), dict(kwargs)))
            return "published"
        return real_git(repository, args, **kwargs)

    monkeypatch.setattr(publish, "_git", capture_push)
    monkeypatch.setattr(publish, "_revalidate_plan", capture_revalidation)
    service.close()
    restarted_service, restarted_invocation = _service(tmp_path)
    result = restarted_service.invoke(
        publish.PUSH_OPERATION,
        prepared,
        restarted_invocation,
    )
    assert result["published"] is True
    assert revalidations == 1
    assert result["output"] == "Git publication completed"
    assert plan["push_url"] not in result["output"]
    assert calls == [
        (
            calls[0][0],
            [
                "push",
                "--force-with-lease=refs/heads/main:" + "0" * 40,
                "--",
                "https://example.test/org/repository.git",
                f"{source_oid}:refs/heads/main",
            ],
            {"timeout": 180, "hardened": True},
        )
    ]
    assert len(calls) == 1


def test_v4_push_revalidates_workspace_repository_ref_and_url(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _repository(tmp_path)
    service, invocation = _service(tmp_path)
    prepared = service.invoke(
        publish.PREPARE_OPERATION,
        {"workspace_id": "workspace-a", "remote": "origin", "branch": "main"},
        invocation,
    )
    subprocess.run(
        [
            "git",
            "-C",
            tmp_path,
            "remote",
            "set-url",
            "origin",
            "https://attacker.invalid/repository.git",
        ],
        check=True,
    )
    monkeypatch.setattr(
        publish,
        "_execute_force_with_lease",
        lambda *_args, **_kwargs: pytest.fail("push executor must not run"),
    )
    with pytest.raises(PermissionError, match="compare-and-swap"):
        service.invoke(publish.PUSH_OPERATION, prepared, invocation)
    invocation.client.revision = 2
    with pytest.raises(PermissionError, match="mount revision"):
        service.invoke(publish.PUSH_OPERATION, prepared, invocation)


def test_v4_push_uses_the_host_credential_port_without_exposing_material(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _repository(tmp_path)
    credential_identity = _credential_identity()
    service, invocation = _service(
        tmp_path,
        credential_identity=credential_identity,
    )
    prepared = service.invoke(
        publish.PREPARE_OPERATION,
        {
            "workspace_id": "workspace-a",
            "remote": "origin",
            "branch": "main",
        },
        invocation,
    )
    plan = prepared["plan"]
    assert plan["credential_transport"] == {
        "mode": "host-bound-https",
        "credential_identity": credential_identity,
    }
    direct_pushes: list[list[str]] = []
    real_git = publish._git

    def no_direct_authenticated_push(
        repository: Path,
        args: list[str],
        **kwargs: Any,
    ) -> str:
        if args[:1] == ["push"]:
            direct_pushes.append(list(args))
            pytest.fail("credentialed push must use the Host credential port")
        return real_git(repository, args, **kwargs)

    monkeypatch.setattr(publish, "_git", no_direct_authenticated_push)
    result = service.invoke(publish.PUSH_OPERATION, prepared, invocation)

    assert result["published"] is True
    assert direct_pushes == []
    assert invocation.client.credential_pushes == [
        {
            "git_executable": publish._git_executable(),
            "git_executable_identity": plan["host_toolchain"]["git"],
            "bare_repository": invocation.client.credential_pushes[0][
                "bare_repository"
            ],
            "remote_url": "https://example.test/org/repository.git",
            "refspec": plan["refspec"],
            "force_with_lease": plan["force_with_lease"]["argument"],
            "credential_handle": "credential:opaque-git-push",
            "provider_instance_id": "git-publish.service",
            "credential_scope": "git.publish",
            "workspace_id": "workspace-a",
            "selection_receipt": "credential-selection:test-2",
        }
    ]


def test_v4_private_credential_identity_survives_provider_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _repository(tmp_path)
    identity = _credential_identity()
    service, invocation = _service(tmp_path, credential_identity=identity)
    prepared = service.invoke(
        publish.PREPARE_OPERATION,
        {"workspace_id": "workspace-a", "remote": "origin", "branch": "main"},
        invocation,
    )
    service.close()
    restarted, restarted_invocation = _service(
        tmp_path,
        credential_identity=identity,
    )
    monkeypatch.setattr(
        publish,
        "_execute_force_with_lease",
        lambda **_kwargs: "published by restarted provider",
    )

    result = restarted.invoke(publish.PUSH_OPERATION, prepared, restarted_invocation)

    assert result["published"] is True
    assert result["output"] == "published by restarted provider"


def test_v4_rejects_browser_credential_handle_before_host_selection(
    tmp_path: Path,
) -> None:
    _repository(tmp_path)
    service, invocation = _service(tmp_path)

    with pytest.raises(PermissionError, match="client-supplied"):
        service.invoke(
            publish.PREPARE_OPERATION,
            {
                "workspace_id": "workspace-a",
                "remote": "origin",
                "branch": "main",
                "credential_handle": "credential:browser-chosen",
            },
            invocation,
        )

    assert invocation.client.calls == []
    assert invocation.client.credential_selections == []


def test_v4_input_schema_rejects_browser_credential_handle() -> None:
    pack_root = (
        Path(__file__).resolve().parents[1]
        / "ecosystem"
        / "rumi_git_publish_pack"
    )
    executable_catalog = json.loads(
        (pack_root / "executables.v4.json").read_text(encoding="utf-8")
    )
    operation_schemas = [
        operation["input_schema"]
        for variant in executable_catalog["variants"]
        for operation in variant["operations"]
    ]

    assert len(operation_schemas) == 2
    for schema in operation_schemas:
        assert schema["additionalProperties"] is False
        assert "credential_handle" not in schema["properties"]
        assert not Draft202012Validator(schema).is_valid(
            {
                "workspace_id": "workspace-a",
                "remote": "origin",
                "branch": "main",
                "credential_handle": "credential:browser-chosen",
            }
        )


def test_v4_push_fails_if_host_credential_selection_changes_after_prepare(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _repository(tmp_path)
    service, invocation = _service(
        tmp_path,
        credential_identity=_credential_identity(),
    )
    prepared = service.invoke(
        publish.PREPARE_OPERATION,
        {"workspace_id": "workspace-a", "remote": "origin", "branch": "main"},
        invocation,
    )
    invocation.client.credential_identity = _credential_identity(
        handle="credential:replacement"
    )
    monkeypatch.setattr(
        publish,
        "_execute_force_with_lease",
        lambda *_args, **_kwargs: pytest.fail("push executor must not run"),
    )

    with pytest.raises(PermissionError, match="compare-and-swap"):
        service.invoke(publish.PUSH_OPERATION, prepared, invocation)


def test_v4_push_fails_if_host_credential_is_revoked_after_prepare(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _repository(tmp_path)
    service, invocation = _service(
        tmp_path,
        credential_identity=_credential_identity(),
    )
    prepared = service.invoke(
        publish.PREPARE_OPERATION,
        {"workspace_id": "workspace-a", "remote": "origin", "branch": "main"},
        invocation,
    )
    invocation.client.credential_identity = None
    monkeypatch.setattr(
        publish,
        "_execute_force_with_lease",
        lambda *_args, **_kwargs: pytest.fail("push executor must not run"),
    )

    with pytest.raises(PermissionError, match="compare-and-swap"):
        service.invoke(publish.PUSH_OPERATION, prepared, invocation)


def test_v4_rejects_client_authority_upstream_and_ambient_git_controls(
    tmp_path: Path,
) -> None:
    _repository(tmp_path)
    service, invocation = _service(tmp_path)
    with pytest.raises(PermissionError, match="client-supplied"):
        service.invoke(
            publish.PREPARE_OPERATION,
            {
                "workspace_id": "workspace-a",
                "remote": "origin",
                "branch": "main",
                "approved": True,
                "authority_receipt": "forged",
            },
            invocation,
        )
    assert invocation.client.calls == []
    with pytest.raises(PermissionError, match="upstream"):
        service.invoke(
            publish.PREPARE_OPERATION,
            {
                "workspace_id": "workspace-a",
                "remote": "origin",
                "branch": "main",
                "set_upstream": True,
            },
            invocation,
        )
    subprocess.run(
        ["git", "-C", tmp_path, "config", "core.sshCommand", "malicious-command"],
        check=True,
    )
    with pytest.raises(PermissionError, match="execution controls"):
        service.invoke(
            publish.PREPARE_OPERATION,
            {"workspace_id": "workspace-a", "remote": "origin", "branch": "main"},
            invocation,
        )
    environment = publish._hardened_git_environment()
    assert "HOME" not in environment
    assert "SSH_AUTH_SOCK" not in environment
    assert environment["GIT_CONFIG_NOSYSTEM"] == "1"
    assert environment["GIT_PROTOCOL_FROM_USER"] == "0"
    assert environment["GIT_TERMINAL_PROMPT"] == "0"
    with pytest.raises(PermissionError, match="remote (?:URL|host)"):
        publish._remote_host("git@-oProxyCommand=malicious:repository.git")


def test_v4_ssh_requires_unavailable_host_credential_port(tmp_path: Path) -> None:
    _repository(tmp_path)
    subprocess.run(
        [
            "git",
            "-C",
            tmp_path,
            "remote",
            "set-url",
            "origin",
            "git@example.test:org/repository.git",
        ],
        check=True,
    )
    service, invocation = _service(tmp_path)
    prepared = service.invoke(
        publish.PREPARE_OPERATION,
        {"workspace_id": "workspace-a", "remote": "origin", "branch": "main"},
        invocation,
    )
    with pytest.raises(PermissionError, match="HOST_CREDENTIAL_PORT_UNAVAILABLE"):
        service.invoke(publish.PUSH_OPERATION, prepared, invocation)
    assert invocation.client.credential_selections == []


def test_host_factories_each_require_one_exact_operation(tmp_path: Path) -> None:
    bindings = []
    domains = {}
    identities = (
        (publish.PREPARE_OPERATION, publish.PREPARE_FUNCTION_ID),
        (publish.PUSH_OPERATION, publish.FUNCTION_ID),
    )
    for index, (operation_id, function_id) in enumerate(
        identities,
        start=1,
    ):
        principal = "sha256:" + str(index + 3) * 64
        binding = SimpleNamespace(
            operation=SimpleNamespace(
                contract_id="tobkiri.service.git.publish.v1",
                contract_version="1.0.0",
                operation_id=operation_id,
            ),
            principal_ref=SimpleNamespace(value=principal),
            artifact=SimpleNamespace(digest="sha256:" + "e" * 64),
            function=SimpleNamespace(
                function_id=function_id,
                implementation_digest="sha256:" + "f" * 64,
            ),
        )
        bindings.append(binding)
        domains[(binding.operation.contract_id, operation_id, principal)] = (
            f"domain-git-publish-{index}"
        )
    assert set(publish.HOST_PROVIDER_FACTORY) == {
        publish.PREPARE_FUNCTION_ID,
        publish.FUNCTION_ID,
    }
    for binding, (_, function_id) in zip(bindings, identities, strict=True):
        context = SimpleNamespace(
            profile_id="profile-a",
            plan_digest="sha256:" + "1" * 64,
            security_epoch=2,
            state_root=tmp_path,
            provider_bindings=(binding,),
            domain_ids=domains,
        )
        captured = publish.HOST_PROVIDER_FACTORY[function_id].capture(context)
        assert len(captured.contributions) == 1
        assert captured.contributions[0].operation_id == binding.operation.operation_id
        captured.close()

    prepare_factory = publish.HOST_PROVIDER_FACTORY[publish.PREPARE_FUNCTION_ID]
    context = SimpleNamespace(
        profile_id="profile-a",
        plan_digest="sha256:" + "1" * 64,
        security_epoch=2,
        state_root=tmp_path,
        provider_bindings=tuple(bindings),
        domain_ids=domains,
    )
    with pytest.raises(PermissionError, match="incomplete"):
        prepare_factory.capture(context)
    with pytest.raises(PermissionError, match="incomplete"):
        prepare_factory.capture(
            SimpleNamespace(**{**vars(context), "provider_bindings": (bindings[1],)})
        )
    bindings[0].operation.contract_id = "attacker.contract.git.publish.v1"
    with pytest.raises(PermissionError, match="incomplete"):
        prepare_factory.capture(
            SimpleNamespace(**{**vars(context), "provider_bindings": (bindings[0],)})
        )
