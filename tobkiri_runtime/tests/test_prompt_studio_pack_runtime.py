from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from core_runtime.capability_binding_registration import (
    register_pack_binding_handlers,
)
from core_runtime.interface_registry import InterfaceRegistry
from core_runtime.resolved_profile import ResolutionInput, resolve_profile
from ecosystem.rumi_prompt_studio_pack.runtime.service import PromptStudioService
from ecosystem.rumi_prompt_studio_pack.runtime.store import (
    PromptStudioStore,
    PromptWriteConflict,
)


def _hash(body: str) -> str:
    return "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()


def test_save_versions_and_stale_write_are_atomic(tmp_path: Path) -> None:
    store = PromptStudioStore("fixture", user_data_root=tmp_path)

    created = store.save(
        "system.main",
        "first",
        expected_body_hash=_hash(""),
    )
    updated = store.save(
        "system.main",
        "second",
        expected_body_hash=_hash("first"),
    )

    assert created["prompt"]["body"] == "first"
    assert updated["prompt"]["body"] == "second"
    assert store.versions("system.main")["count"] == 2
    with pytest.raises(PromptWriteConflict):
        store.save(
            "system.main",
            "stale",
            expected_body_hash=_hash("first"),
        )
    assert store.get("system.main")["body"] == "second"


def test_first_write_rollback_removes_override(tmp_path: Path) -> None:
    store = PromptStudioStore("fixture", user_data_root=tmp_path)
    created = store.save(
        "system.main",
        "override",
        expected_body_hash=_hash(""),
    )

    result = store.rollback(
        "system.main",
        created["version"]["version_id"],
        expected_body_hash=_hash("override"),
    )

    assert result["removed_override"] is True
    assert store.get("system.main") is None


def test_composition_edge_state_uses_the_same_atomic_owner_store(
    tmp_path: Path,
) -> None:
    store = PromptStudioStore("fixture", user_data_root=tmp_path)

    result = store.set_edge_state("system-to-model", False)

    assert result["enabled"] is False
    assert store.snapshot()["edge_states"] == {"system-to-model": False}


def test_migration_preserves_legacy_data_and_can_rollback(tmp_path: Path) -> None:
    legacy = tmp_path / "profiles" / "fixture" / "prompts"
    legacy.mkdir(parents=True)
    source = legacy / "system.main.system.md"
    source.write_text("legacy body", encoding="utf-8")
    store = PromptStudioStore("fixture", user_data_root=tmp_path)

    inspection = store.inspect_migration()
    marker = store.migrate_from_legacy(
        expected_source_hash=inspection.source_hash,
    )

    assert source.read_text(encoding="utf-8") == "legacy body"
    assert store.get("system.main")["body"] == "legacy body"
    assert Path(marker["backup"]).is_dir()

    store.rollback_migration(marker["migration_id"])
    assert source.read_text(encoding="utf-8") == "legacy body"
    assert not store.path.exists()
    assert not store.owner_marker.exists()


def test_migration_rejects_changed_source_and_foreign_root(tmp_path: Path) -> None:
    legacy = tmp_path / "profiles" / "fixture" / "prompts"
    legacy.mkdir(parents=True)
    source = legacy / "system.main.md"
    source.write_text("before", encoding="utf-8")
    store = PromptStudioStore("fixture", user_data_root=tmp_path)
    inspection = store.inspect_migration()
    source.write_text("after", encoding="utf-8")

    with pytest.raises(PromptWriteConflict):
        store.migrate_from_legacy(
            expected_source_hash=inspection.source_hash,
        )
    with pytest.raises(PermissionError):
        store.inspect_migration(tmp_path / "foreign")


def test_record_migration_is_atomic_and_legacy_payload_is_unchanged(
    tmp_path: Path,
) -> None:
    records = [
        {
            "prompt_id": "system.main",
            "body": "legacy body",
            "description": "fixture",
            "variables": ["name"],
            "enabled": True,
            "source": "defaultspack-shared-json",
        }
    ]
    before = json.loads(json.dumps(records))
    source_hash = _hash(
        json.dumps(
            {"records": records, "edge_states": {}},
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    store = PromptStudioStore("fixture", user_data_root=tmp_path)

    marker = store.migrate_records(
        records,
        expected_source_hash=source_hash,
    )

    assert records == before
    assert store.get("system.main")["body"] == "legacy body"
    assert Path(marker["backup"]).joinpath("legacy-records.json").is_file()
    store.rollback_migration(marker["migration_id"])
    assert not store.path.exists()


def test_record_migration_refuses_initialized_target(tmp_path: Path) -> None:
    store = PromptStudioStore("fixture", user_data_root=tmp_path)
    store.save("existing", "body", expected_body_hash=_hash(""))
    records = [{"prompt_id": "legacy", "body": "body"}]
    source_hash = _hash(
        json.dumps(
            {
                "records": [
                    {
                        "prompt_id": "legacy",
                        "body": "body",
                        "description": "",
                        "variables": [],
                        "enabled": True,
                        "source": "legacy",
                    }
                ],
                "edge_states": {},
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )

    with pytest.raises(RuntimeError, match="already initialized"):
        store.migrate_records(records, expected_source_hash=source_hash)


def test_provider_free_testbench_and_redacted_versions(tmp_path: Path) -> None:
    service = PromptStudioService(user_data_root=tmp_path)
    created = service.invoke(
        "save",
        {
            "profile_id": "fixture",
            "prompt_id": "system.main",
            "body": "Hello {{name}}",
            "expected_body_hash": _hash(""),
            "metadata": {"secret": "must-not-persist", "label": "safe"},
        },
    )

    result = service.invoke(
        "test",
        {
            "profile_id": "fixture",
            "prompt_id": "system.main",
            "variables": {"name": "Rumi"},
        },
    )
    versions = service.invoke(
        "versions",
        {"profile_id": "fixture", "prompt_id": "system.main"},
    )

    assert result["rendered"] == "Hello Rumi"
    assert result["provider_invoked"] is False
    assert result["tool_invoked"] is False
    assert "previous_body" not in json.dumps(versions)
    stored = PromptStudioStore("fixture", user_data_root=tmp_path).get(
        "system.main"
    )
    assert "secret" not in stored["metadata"]
    assert created["prompt"]["body_hash"] == _hash("Hello {{name}}")


def test_v3_process_providers_register_without_importing_pack_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Approval:
        @staticmethod
        def is_pack_approved_and_verified(pack_id: str):
            return pack_id == "rumi_prompt_studio_pack", "fixture"

    registry = InterfaceRegistry()
    ecosystem = Path(__file__).resolve().parents[1] / "ecosystem"
    monkeypatch.setenv("RUMI_ALLOW_HOST_EXECUTION", "true")

    result = register_pack_binding_handlers(
        interface_registry=registry,
        approval_manager=Approval(),
        ecosystem_dir=str(ecosystem),
        effective_pack_ids=("rumi_prompt_studio_pack",),
    )

    assert result.ok is True
    providers = registry.get(
        "global_contract.provider.rumi.action.prompt.author.v1",
        strategy="all",
    )
    assert len(providers) == 1
    assert providers[0]["isolation"] == "process"


def test_resolved_profile_selects_manifest_only_prompt_providers() -> None:
    ecosystem = Path(__file__).resolve().parents[1] / "ecosystem"
    plan = resolve_profile(
        ResolutionInput(
            profile_id="fixture",
            profile_revision="r1",
            platform="test",
            policy_revision="p1",
            lockfile_revision=None,
            requested_pack_ids=("rumi_prompt_studio_pack",),
            authorized_pack_ids=("rumi_prompt_studio_pack",),
            healthy_pack_ids=("rumi_prompt_studio_pack",),
            policy_capabilities=(
                "profile.prompt.author.read",
                "profile.prompt.author.write",
                "profile.prompt.author.migrate",
            ),
        ),
        ecosystem_dir=ecosystem,
    )

    assert "rumi_prompt_studio_pack" in plan.effective_pack_set
    assert set(plan.effective_permissions) == {
        "profile.prompt.author.read",
        "profile.prompt.author.write",
        "profile.prompt.author.migrate",
    }
    assert {
        item.contract_id for item in plan.providers
    } >= {
        "rumi.resource.prompt.studio.v1",
        "rumi.action.prompt.author.v1",
        "rumi.action.prompt.version.v1",
        "rumi.action.prompt.test.v1",
        "rumi.action.prompt.migrate.v1",
    }

