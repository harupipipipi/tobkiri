"""Tool registry Host boundaries with isolated data; Broker tests are separate."""

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from core_runtime.host_provider_backend_v4 import HostProviderCaptureContextV4
from ecosystem.rumi_tool_registry_pack.runtime import host
from tests.test_mcp_connection_owner import _Invocation
from tests.test_pack_architecture_wave6 import _definition
from tobkiri_host.models import OpaqueAuthorityRef


class Contributions:
    def __init__(self):
        self.items = []
        self.calls = []

    def providers(self, contract):
        assert contract == host.CONTRIBUTION
        return tuple(item[0] for item in self.items)

    def invoke(self, contract, operation, payload, *, provider_instance_id):
        self.calls.append((contract, operation, dict(payload), provider_instance_id))
        return next(
            value
            for metadata, value in self.items
            if metadata["provider_instance_id"] == provider_instance_id
        )


@pytest.fixture
def registry_host(tmp_path):
    client = Contributions()

    def invoke(kind, payload, *, change=None):
        function = f"{host.PACK_ID}.tool-registry.{kind}"
        contract, operation = host._BINDINGS[function]
        binding = SimpleNamespace(
            function=SimpleNamespace(
                function_id=function, implementation_digest="impl"
            ),
            operation=SimpleNamespace(
                contract_id=contract, operation_id=operation, contract_version="1.0.0"
            ),
            principal_ref=OpaqueAuthorityRef("principal:" + kind),
            artifact=SimpleNamespace(digest="artifact"),
        )
        capture = HostProviderCaptureContextV4(
            profile_id="defaults",
            plan_digest="plan",
            security_epoch=1,
            activation={"activation_id": "active"},
            state_root=tmp_path,
            provider_bindings=(binding,),
            catalog_bindings=(binding,),
            domain_ids={(contract, operation, binding.principal_ref.value): "domain"},
            user_data_root=tmp_path,
        )
        provider = host.HOST_PROVIDER_FACTORY[function].capture(capture)
        invocation = _Invocation(operation, payload)
        invocation.envelope = replace(
            invocation.envelope,
            contract_id=contract,
            target_principal=binding.principal_ref,
        )
        if change:
            invocation.envelope = replace(invocation.envelope, **change)

        def contract_client(**kwargs):
            assert kwargs == {
                "allowed_contract_ids": frozenset({host.CONTRIBUTION})
                if kind == "definition"
                else frozenset(),
                "consumer_pack_id": host.PACK_ID,
                "include_credentials": False,
            }
            return client

        invocation.contract_client = contract_client
        try:
            return provider.contributions[0].invoke(operation, payload, invocation)
        finally:
            provider.close()

    return invoke, client


def test_registry_has_one_captured_owner_and_revision_guard(registry_host, tmp_path):
    invoke, _ = registry_host
    assert invoke("definition", {"operation": "list"})["definitions"] == []
    assert not (tmp_path / "packs").exists(), "read created owner state"
    saved = invoke(
        "manage",
        {"operation": "save", "definition": _definition(), "expected_revision": 0},
    )
    assert saved["registry_revision"] == 1
    invoke(
        "manage",
        {
            "operation": "alias",
            "alias": "short",
            "target_tool_id": "sample.read",
            "expected_revision": 1,
        },
    )
    result = invoke("definition", {"operation": "resolve", "tool_id": "short"})
    assert result["found"] and result["definition"]["tool_id"] == "sample.read"
    with pytest.raises(RuntimeError, match="stale"):
        invoke(
            "manage",
            {"operation": "delete", "tool_id": "sample.read", "expected_revision": 1},
        )
    invoke(
        "manage",
        {"operation": "delete", "tool_id": "sample.read", "expected_revision": 2},
    )
    assert invoke("definition", {"operation": "get", "tool_id": "short"}) == {
        "found": False
    }
    assert not (tmp_path / "packs" / host.PACK_ID / "profiles" / "other").exists()


@pytest.mark.parametrize(
    "change",
    [
        {"profile_id": "other"},
        {"profile_id": None},
        {"approved": True},
        {"expected_revision": True},
        {"expected_revision": "0"},
        {"expected_revision": -1},
        {"user_data_root": "/tmp/foreign"},
        {"caller_id": "forged"},
        {"_contract_consumer_pack_id": "forged"},
        {"operation": "migrate"},
    ],
)
def test_mutation_rejects_scope_fields_and_bad_revision(
    registry_host, tmp_path, change
):
    invoke, client = registry_host
    with pytest.raises((PermissionError, ValueError)):
        invoke(
            "manage",
            {
                "operation": "save",
                "definition": _definition(),
                "expected_revision": 0,
                **change,
            },
        )
    assert not (tmp_path / "packs").exists()
    assert client.calls == []


@pytest.mark.parametrize(
    "change",
    [
        {"target_principal": OpaqueAuthorityRef("foreign")},
        {"contract_id": "other.contract"},
        {"operation_id": "other.operation"},
        {"contract_version": "2.0.0"},
        {"payload": {"operation": "save"}},
        {
            "context": SimpleNamespace(
                profile_id="other",
                activation_id="active",
                plan_digest="plan",
                security_epoch=1,
            )
        },
    ],
)
def test_changed_envelope_is_rejected_before_any_owner_access(
    registry_host, tmp_path, change
):
    invoke, _ = registry_host
    with pytest.raises(PermissionError, match="changed"):
        invoke("definition", {"operation": "list"}, change=change)
    assert not (tmp_path / "packs").exists()


def test_contribution_uses_selected_canonical_operation(registry_host):
    invoke, client = registry_host
    client.items = [
        (
            {
                "provider_instance_id": "selected.tools",
                "operation_id": "selected.tools.read",
                "content_hash": "captured",
            },
            {"definitions": [_definition()], "aliases": {"short": "sample.read"}},
        )
    ]
    result = invoke("definition", {"operation": "resolve", "tool_id": "short"})
    assert result["found"] and result["resolved_tool_id"] == "sample.read"
    assert client.calls == [
        (
            host.CONTRIBUTION,
            "selected.tools.read",
            {"profile_id": "defaults"},
            "selected.tools",
        )
    ]


def test_contribution_cannot_fall_back_to_legacy_list(registry_host):
    invoke, client = registry_host
    client.items = [({"provider_instance_id": "selected.tools"}, {})]
    with pytest.raises(RuntimeError, match="operation is unavailable"):
        invoke("definition", {"operation": "list"})
    assert not client.calls


def test_migration_preserves_data_and_redacts_backup_location(registry_host, tmp_path):
    import hashlib
    import json

    invoke, _ = registry_host
    source = {"definitions": [_definition()], "aliases": {"short": "sample.read"}}
    digest = hashlib.sha256(
        json.dumps(source, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    migrated = invoke(
        "migrate", {"operation": "migrate", **source, "expected_source_hash": digest}
    )
    snapshot = invoke("definition", {"operation": "list"})
    assert snapshot["migration"] == {
        "migration_id": migrated["migration_id"],
        "source_hash": digest,
    }
    stored = json.loads(next(tmp_path.rglob("tool-definitions.json")).read_text())
    assert Path(stored["migration"]["backup"]).is_dir()
    invoke(
        "migrate",
        {
            "operation": "rollback",
            "migration_id": migrated["migration_id"],
            "expected_revision": snapshot["revision"],
        },
    )
    assert invoke("definition", {"operation": "list"})["definitions"] == []


def test_registry_uses_real_broker_profile_grants_and_rejects_undeclared_write(
    tmp_path,
    monkeypatch,
):
    from core_runtime.authority.v4 import AuthorityDenied
    from tests.conformance_support.host_profile import captured_host_profile
    from tests.test_mcp_host_broker import _edge
    from tests.test_production_frontend_contract_http import _ShellPolicyPackVmBackend
    from tobkiri_host.errors import ProviderExecutionError, ResolutionError

    read_function = f"{host.PACK_ID}.tool-registry.definition"
    write_function = f"{host.PACK_ID}.tool-registry.manage"
    read_contract, read_operation = host._BINDINGS[read_function]
    write_contract, write_operation = host._BINDINGS[write_function]
    migrate_contract, migrate_operation = host._BINDINGS[
        f"{host.PACK_ID}.tool-registry.migrate"
    ]
    edges = [
        _edge("shell.tauri.default", read_function, read_contract, read_operation),
        _edge("shell.tauri.default", write_function, write_contract, write_operation),
    ]
    with captured_host_profile(
        tmp_path,
        monkeypatch,
        packs=(host.PACK_ID,),
        edges=edges,
        backends=(_ShellPolicyPackVmBackend(),),
    ) as (session, _authority):

        def invoke(contract, operation, payload):
            # This value is the test Host's authenticated presentation binding.
            return session.invoke(
                contract,
                operation,
                {**payload, "_session_id": "registry-owner-session"},
            )

        with pytest.raises(ValueError, match="authenticated session binding"):
            session.invoke(read_contract, read_operation, {"operation": "list"})
        root = tmp_path / "user-data" / "packs" / host.PACK_ID
        listed = invoke(read_contract, read_operation, {"operation": "list"})
        assert listed["profile_id"] == "defaults" and listed["definitions"] == []
        assert not root.exists(), "resource read initialized persistence"
        request = {
            "operation": "save",
            "definition": _definition(),
            "expected_revision": 0,
        }
        with pytest.raises((ProviderExecutionError, ResolutionError)):
            invoke(write_contract, write_operation, {**request, "profile_id": "other"})
        with pytest.raises((ProviderExecutionError, ResolutionError)):
            invoke(write_contract, write_operation, {**request, "approved": True})
        with pytest.raises(AuthorityDenied):
            invoke(
                migrate_contract,
                migrate_operation,
                {
                    "operation": "rollback",
                    "migration_id": "migration-" + "0" * 32,
                    "expected_revision": 0,
                },
            )
        assert not root.exists()
        saved = invoke(write_contract, write_operation, request)
        assert saved["registry_revision"] == 1
        result = invoke(
            read_contract,
            read_operation,
            {"operation": "resolve", "tool_id": "sample.read"},
        )
        assert result["found"] and result["definition"]["tool_id"] == "sample.read"
        with pytest.raises(ProviderExecutionError):
            invoke(write_contract, write_operation, request)
        listed = invoke(read_contract, read_operation, {"operation": "list"})
        assert listed["revision"] == 1 and len(listed["definitions"]) == 1
        assert not (root / "profiles" / "other").exists()


def test_cancelled_registry_lock_wait_never_publishes_a_definition(tmp_path):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from core_runtime.runtime_locks import NamedLock
    from ecosystem.rumi_tool_registry_pack.runtime.registry import (
        ToolDefinitionRegistry,
    )

    entered, cancelled = threading.Event(), threading.Event()

    def guard():
        entered.set()
        if cancelled.is_set():
            raise InterruptedError("captured invocation cancelled")

    registry = ToolDefinitionRegistry("defaults", user_data_root=tmp_path, guard=guard)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with NamedLock(registry.lock_root, "tool-definitions"):
            pending = pool.submit(registry.save, _definition(), 0)
            assert entered.wait(2)
            cancelled.set()
            with pytest.raises(InterruptedError, match="cancelled"):
                pending.result(timeout=2)
            assert not registry.path.exists()
    assert not (registry.lock_root / "tool-definitions.lock").exists()


def test_concurrent_host_updates_preserve_one_revision(registry_host):
    from concurrent.futures import ThreadPoolExecutor
    import threading

    invoke, _ = registry_host
    ready = threading.Barrier(2)

    def save(tool_id):
        ready.wait(timeout=2)
        try:
            return invoke(
                "manage",
                {
                    "operation": "save",
                    "definition": _definition(tool_id),
                    "expected_revision": 0,
                },
            )["registry_revision"]
        except RuntimeError as error:
            assert "stale" in str(error)
            return "stale"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(save, ["first", "second"]))
    assert results.count(1) == 1 and results.count("stale") == 1
    snapshot = invoke("definition", {"operation": "list"})
    assert snapshot["revision"] == 1 and len(snapshot["definitions"]) == 1


def test_migration_rejects_duplicate_definitions_before_initialization(
    registry_host, tmp_path
):
    import hashlib
    import json

    invoke, _ = registry_host
    source = {"definitions": [_definition(), _definition()], "aliases": {}}
    digest = hashlib.sha256(
        json.dumps(source, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    with pytest.raises(ValueError, match="duplicate"):
        invoke(
            "migrate",
            {"operation": "migrate", **source, "expected_source_hash": digest},
        )
    assert not (tmp_path / "packs").exists()


def test_definition_cannot_shadow_an_existing_alias(registry_host, tmp_path):
    invoke, _ = registry_host
    invoke(
        "manage",
        {"operation": "save", "definition": _definition(), "expected_revision": 0},
    )
    invoke(
        "manage",
        {
            "operation": "alias",
            "alias": "short",
            "target_tool_id": "sample.read",
            "expected_revision": 1,
        },
    )
    path = next(tmp_path.rglob("tool-definitions.json"))
    before = path.read_bytes()
    with pytest.raises(ValueError, match="collides with an alias"):
        invoke(
            "manage",
            {
                "operation": "save",
                "definition": _definition("short"),
                "expected_revision": 2,
            },
        )
    assert path.read_bytes() == before
    resolved = invoke("definition", {"operation": "resolve", "tool_id": "short"})
    assert resolved["definition"]["tool_id"] == "sample.read"
    assert resolved["registry_revision"] == 2


def test_stale_rollback_preserves_updates_and_matching_rollback_backs_them_up(
    registry_host,
    tmp_path,
):
    import hashlib
    import json

    from ecosystem.rumi_tool_registry_pack.runtime.registry import (
        ToolDefinitionRegistry,
    )

    invoke, _ = registry_host
    source = {"definitions": [_definition()], "aliases": {}}
    digest = hashlib.sha256(
        json.dumps(source, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    migrated = invoke(
        "migrate", {"operation": "migrate", **source, "expected_source_hash": digest}
    )
    invoke(
        "manage",
        {
            "operation": "save",
            "definition": _definition("later"),
            "expected_revision": 1,
        },
    )
    path = next(tmp_path.rglob("tool-definitions.json"))
    before = path.read_bytes()
    request = {"operation": "rollback", "migration_id": migrated["migration_id"]}
    with pytest.raises(RuntimeError, match="stale"):
        invoke("migrate", {**request, "expected_revision": 1})
    with pytest.raises(PermissionError, match="payload is invalid"):
        invoke("migrate", request)
    # The old one-argument API also cannot discard post-migration updates.
    legacy = ToolDefinitionRegistry("defaults", user_data_root=tmp_path)
    with pytest.raises(RuntimeError, match="stale"):
        legacy.rollback_migration(migrated["migration_id"])
    assert path.read_bytes() == before
    assert not list(tmp_path.rglob("rollback-*.json"))
    assert invoke("migrate", {**request, "expected_revision": 2})["rolled_back"]
    assert not path.exists()
    backup = next(tmp_path.rglob("rollback-*.json"))
    assert json.loads(backup.read_bytes()) == json.loads(before)
