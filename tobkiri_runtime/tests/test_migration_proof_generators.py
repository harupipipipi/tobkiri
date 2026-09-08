"""Focused tests for independent migration source and proof generators."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts.generate_executable_source_registry_v1 import (
    ECOSYSTEM,
    ExecutableSourceRegistryError,
    build_registry,
)
from scripts.quality.run_independent_migration_proof import (
    build_proof,
)


def _write_empty_source_fixture(path: Path) -> Path:
    """Write the smallest valid explicit-source fixture."""

    path.write_text(
        json.dumps(
            {
                "schema": "io.tobkiri.legacy-executable-source-input.v1",
                "source_format": "legacy-entrypoint-map",
                "operation_id_overrides": [],
                "packs": {},
            }
        ),
        encoding="utf-8",
    )
    return path


def _write_v3_pack(
    ecosystem_root: Path,
    *,
    module_path: str,
    implementation_bytes: bytes,
) -> Path:
    """Write a minimal v3 Pack declaring one Python entrypoint."""

    pack_root = ecosystem_root / "sample_pack"
    pack_root.mkdir(parents=True)
    module = ".".join(Path(module_path).with_suffix("").parts)
    (pack_root / "rumi.pack.v3.json").write_text(
        json.dumps(
            {
                "contracts": {
                    "provides": [
                        {
                            "id": "rumi.sample.execute.v1",
                            "version": "1.0.0",
                            "provider_instance_id": "sample-provider",
                            "schemas": {
                                "input": {"type": "object"},
                                "output": {"type": "object"},
                                "error": {"type": "object"},
                            },
                        }
                    ]
                },
                "entrypoints": [
                    {
                        "id": "sample.execute",
                        "contract_id": "rumi.sample.execute.v1",
                        "module": f"ecosystem.sample_pack.{module}",
                        "symbol": "execute",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (pack_root / "artifact-manifest.json").write_text(
        json.dumps(
            {
                "artifacts": [
                    {
                        "path": module_path,
                        "sha256": hashlib.sha256(implementation_bytes).hexdigest(),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    return pack_root


def test_source_registry_is_complete_without_v4_catalog_inputs() -> None:
    """Legacy manifests and explicit source records cover all v4 operations."""
    payload = build_registry()
    records = payload["packs"]

    assert payload["source"]["kind"] == "legacy-shaped"
    assert payload["source"]["input_digest"] == _canonical_digest_for_test(
        payload["source"]["inputs"]
    )
    assert payload["source"]["input_paths"] == [
        item["path"] for item in payload["source"]["inputs"]
    ]
    assert len(records) == 175
    assert sum(len(record["operations"]) for record in records.values()) == 237
    for function_id, pack_id, operation_id, implementation_path in (
        ("rumi_command_protocol_pack.catalog.read", "rumi_command_protocol_pack", "command.catalog.read", "runtime/catalog.py"),
        ("tobkiri.ui.settings.read", "tobkiri_ui_settings_pack", "tobkiri_ui_settings_pack.settings-read", "runtime/settings.py"),
        ("tobkiri.ui.catalog.read", "tobkiri_ui_settings_pack", "tobkiri_ui_settings_pack.catalog-read", "runtime/settings.py"),
        ("rumi_conversation_store_pack.conversation-store.resource", "rumi_conversation_store_pack", "rumi_conversation_store_pack.conversation-resource", "runtime/host.py"),
    ):
        record = records[function_id]
        assert record["owner"] == pack_id
        assert record["implementation_path"] == implementation_path
        assert record["implementation_digest"] == "sha256:" + hashlib.sha256(
            (ECOSYSTEM / pack_id / implementation_path).read_bytes()
        ).hexdigest()
        assert [item["operation_id"] for item in record["operations"]] == [operation_id]
    git_write = records["rumi_git_write_pack.git-commit.service"]
    assert [operation["operation_id"] for operation in git_write["operations"]] == [
        "rumi_git_write_pack.git-commit"
    ]
    assert git_write["source"] == [
        {
            "entrypoint_id": "git-write",
            "kind": "legacy-v3-entrypoint",
            "module": "ecosystem.rumi_git_write_pack.runtime.write",
            "path": "tobkiri_runtime/ecosystem/rumi_git_write_pack/rumi.pack.v3.json",
            "symbol": "create_git_write_operation",
        }
    ]
    assert "rumi_git_write_pack.git-write.service" not in records
    git_publish = records["rumi_git_publish_pack.git-publish.service"]
    assert [operation["operation_id"] for operation in git_publish["operations"]] == [
        "rumi_git_publish_pack.git-push"
    ]
    assert "rumi_git_publish_pack.git-publish" not in {
        operation["operation_id"] for operation in git_publish["operations"]
    }
    command = records["rumi_command_protocol_pack.high-risk-command.service"]
    assert command["pack_id"] == "rumi_command_protocol_pack"
    assert command["contract_id"] == "tobkiri.service.command.high-risk.v1"
    assert command["implementation_path"] == "runtime/high_risk_adapter.py"
    assert [operation["operation_id"] for operation in command["operations"]] == [
        "high_risk_command.manage"
    ]
    assert all(
        not path.endswith(("pack.v4.json", "contracts.v4.json", "executables.v4.json"))
        for path in payload["source"]["input_paths"]
    )
    for function_id, record in records.items():
        assert record["function_id"] == function_id
        implementation = ECOSYSTEM / record["pack_id"] / record["implementation_path"]
        assert implementation.is_file()


@pytest.mark.parametrize(
    "mutation",
    ["owner", "contract_id", "implementation_digest", "operation_ids", "stale", "escape", "duplicate", "unknown"],
)
def test_source_registry_rejects_unbound_implementation_adapter(
    tmp_path: Path, mutation: str,
) -> None:
    """Adapters cannot replace another owner, operation, or unpinned byte set."""
    fixture = json.loads(
        Path("tests/fixtures/legacy_executable_sources.v1.json").read_text()
    )
    adapter = fixture["implementation_adapters"][0]
    if mutation in {"owner", "contract_id", "implementation_digest", "operation_ids"}:
        adapter["legacy"][mutation] = "unrelated"
    elif mutation == "stale":
        adapter["implementation_digest"] = "sha256:" + "0" * 64
    elif mutation == "escape":
        adapter["implementation_path"] = "../outside.py"
    elif mutation == "duplicate":
        fixture["implementation_adapters"].append(dict(adapter))
    else:
        adapter["function_id"] = "unknown.function"
    fixture_path = tmp_path / "sources.json"
    fixture_path.write_text(json.dumps(fixture))
    with pytest.raises(ExecutableSourceRegistryError):
        build_registry(fixture_path=fixture_path)


def test_source_registry_retains_adapter_predecessor_evidence() -> None:
    """The new reader keeps the verified predecessor and hashes its own bytes."""
    registry = build_registry()
    record = registry["packs"][
        "rumi_conversation_store_pack.conversation-store.resource"
    ]
    assert any(item["kind"] == "legacy-v3-entrypoint" for item in record["source"])
    adapter = next(
        item for item in record["source"]
        if item["kind"] == "explicit-implementation-adapter"
    )
    assert adapter["legacy_implementation_path"] == "runtime/store.py"
    assert adapter["legacy_implementation_digest"] == "sha256:" + hashlib.sha256(
        (ECOSYSTEM / record["pack_id"] / "runtime/store.py").read_bytes()
    ).hexdigest()
    assert any(
        item["kind"] == "explicit-implementation-adapter"
        and item["digest"] == record["implementation_digest"]
        for item in registry["source"]["inputs"]
    )


def test_source_registry_rejects_unsafe_explicit_implementation_path(
    tmp_path: Path,
) -> None:
    """An explicit legacy source cannot hash bytes outside its Pack."""
    fixture_path = tmp_path / "legacy-executable-sources.json"
    fixture = json.loads(
        Path("tests/fixtures/legacy_executable_sources.v1.json").read_text(
            encoding="utf-8"
        )
    )
    fixture["packs"]["defaultspack"]["entries"][0]["implementation_path"] = "../escape.py"
    fixture_path.write_text(json.dumps(fixture), encoding="utf-8")

    with pytest.raises(ExecutableSourceRegistryError, match="escapes"):
        build_registry(ECOSYSTEM, fixture_path=fixture_path)


def test_source_registry_rejects_duplicate_function_id_override(
    tmp_path: Path,
) -> None:
    """One legacy entrypoint cannot receive two competing Function identities."""
    fixture_path = tmp_path / "legacy-executable-sources.json"
    fixture = json.loads(
        Path("tests/fixtures/legacy_executable_sources.v1.json").read_text(
            encoding="utf-8"
        )
    )
    fixture["function_id_overrides"].append(
        dict(fixture["function_id_overrides"][0])
    )
    fixture_path.write_text(json.dumps(fixture), encoding="utf-8")

    with pytest.raises(ExecutableSourceRegistryError, match="duplicate function ID override"):
        build_registry(ECOSYSTEM, fixture_path=fixture_path)


def test_source_registry_rejects_cross_pack_function_id_override(
    tmp_path: Path,
) -> None:
    """A legacy entrypoint remains owned by the Pack that declares it."""
    fixture_path = tmp_path / "legacy-executable-sources.json"
    fixture = json.loads(
        Path("tests/fixtures/legacy_executable_sources.v1.json").read_text(
            encoding="utf-8"
        )
    )
    fixture["function_id_overrides"][0]["function_id"] = (
        "another_pack.git-commit.service"
    )
    fixture_path.write_text(json.dumps(fixture), encoding="utf-8")

    with pytest.raises(ExecutableSourceRegistryError, match="not owned by Pack"):
        build_registry(ECOSYSTEM, fixture_path=fixture_path)


def test_source_registry_accepts_regular_v3_module_file(tmp_path: Path) -> None:
    """A regular Python module inside its Pack remains a valid v3 source."""

    ecosystem_root = tmp_path / "ecosystem"
    implementation_bytes = b"def execute(payload):\n    return payload\n"
    pack_root = _write_v3_pack(
        ecosystem_root,
        module_path="runtime.py",
        implementation_bytes=implementation_bytes,
    )
    (pack_root / "runtime.py").write_bytes(implementation_bytes)
    fixture_path = _write_empty_source_fixture(tmp_path / "sources.json")

    payload = build_registry(ecosystem_root, fixture_path=fixture_path)

    record = next(iter(payload["packs"].values()))
    assert record["pack_id"] == "sample_pack"
    assert record["implementation_path"] == "runtime.py"


def test_source_registry_rejects_v3_module_file_symlink(tmp_path: Path) -> None:
    """A Pack-local module symlink cannot register bytes outside the Pack."""

    ecosystem_root = tmp_path / "ecosystem"
    implementation_bytes = b"def execute(payload):\n    return payload\n"
    pack_root = _write_v3_pack(
        ecosystem_root,
        module_path="runtime.py",
        implementation_bytes=implementation_bytes,
    )
    outside = tmp_path / "outside-runtime.py"
    outside.write_bytes(implementation_bytes)
    (pack_root / "runtime.py").symlink_to(outside)
    fixture_path = _write_empty_source_fixture(tmp_path / "sources.json")

    with pytest.raises(ExecutableSourceRegistryError, match="symlink"):
        build_registry(ecosystem_root, fixture_path=fixture_path)


def test_source_registry_rejects_v3_intermediate_directory_symlink(
    tmp_path: Path,
) -> None:
    """A symlinked module directory cannot redirect lookup outside the Pack."""

    ecosystem_root = tmp_path / "ecosystem"
    implementation_bytes = b"def execute(payload):\n    return payload\n"
    pack_root = _write_v3_pack(
        ecosystem_root,
        module_path="runtime/execute.py",
        implementation_bytes=implementation_bytes,
    )
    outside_runtime = tmp_path / "outside-runtime"
    outside_runtime.mkdir()
    (outside_runtime / "execute.py").write_bytes(implementation_bytes)
    (pack_root / "runtime").symlink_to(outside_runtime, target_is_directory=True)
    fixture_path = _write_empty_source_fixture(tmp_path / "sources.json")

    with pytest.raises(ExecutableSourceRegistryError, match="symlink"):
        build_registry(ecosystem_root, fixture_path=fixture_path)


def test_independent_proof_preserves_named_identity_and_transactional_receipt() -> None:
    """The proof runner keeps three named users separate through restart."""
    proof = build_proof(observed_head_sha="a" * 40)
    source = proof["source"]
    profile_proof = source["profile_collection_proof"]
    identity = profile_proof["identity_proof"]
    transaction = profile_proof["transaction"]

    assert len(proof["packs"]) == 141
    assert proof["packs"]["tobkiri_ui_settings_pack"]["status"] == "generated-draft"
    assert proof["packs"]["tobkiri_ui_settings_pack"]["semantic_comparison"]["equivalent"] is None
    assert proof["packs"]["rumi_command_protocol_pack"]["status"] == "generated-draft"
    assert identity["all_ids_distinct"] is True
    assert identity["defaults_collapsed"] is False
    assert identity["profile_ids"] == ["profile-aoi", "profile-bora", "profile-cleo"]
    assert transaction["lossless"] is True
    assert transaction["restart_verified"] is True
    assert transaction["replay_rejected_without_mutation"] is True
    statuses = {
        status: sum(entry["status"] == status for entry in proof["packs"].values())
        for status in ("semantically-reviewed", "generated-draft")
    }
    assert statuses == {"semantically-reviewed": 41, "generated-draft": 100}
    assert source["migration_status_counts"] == {
        "generated-draft": 100,
        "release-verified": 0,
        "semantically-reviewed": 41,
    }


def _canonical_digest_for_test(value: object) -> str:
    """Return the registry's canonical digest without importing private code."""

    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()
