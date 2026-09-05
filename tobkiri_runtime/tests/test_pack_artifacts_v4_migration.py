"""Production gates for the one-way Pack v4 artifact migration."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

import scripts.migrate_pack_artifacts_v4 as migration
from scripts.migrate_pack_artifacts_v4 import (
    CATALOG,
    EXCLUDED_PACKS,
    PackV4MigrationError,
    _file_digest,
    _import_record,
    _migration_source_view,
    _render_record,
    _validate_catalog_payload,
    generate,
    import_legacy,
    verify_rendered_artifacts,
)
from tobkiri_protocol.errors import SchemaValidationError


def _catalog() -> dict[str, object]:
    return json.loads(CATALOG.read_text(encoding="utf-8"))


def _explicit_multi_function_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, object]:
    """Build one minimal Pack source using the explicit Function form."""

    record = copy.deepcopy(
        next(item for item in _catalog()["packs"] if item["pack_id"] == "defaultspack")
    )
    pack_root = tmp_path / "ecosystem" / "test_pack"
    implementation = pack_root / "runtime" / "implementation.py"
    implementation.parent.mkdir(parents=True)
    implementation.write_text("def invoke() -> None:\n    return None\n", encoding="utf-8")
    implementation_digest = _file_digest(implementation)
    monkeypatch.setattr(migration, "ECOSYSTEM", pack_root.parent)

    record.update(
        {
            "pack_id": "test_pack",
            "display_name": "Test Pack",
            "capabilities": ["shell.execute", "shell.inspect"],
            "runtime_artifacts": [
                {
                    "path": "runtime/implementation.py",
                    "digest": implementation_digest,
                    "kind": "executable",
                }
            ],
            "source_provenance": {
                "owner": "test_pack",
                "mode": "canonical-v4",
                "source_format": "test",
            },
        }
    )
    contract = record["provided_contracts"][0]
    contract.update(
        {
            "contract_id": "tobkiri.service.test.v1",
            "owner": "test_pack",
            "provider_id": "test_pack.legacy.provider",
            "required_capabilities": ["shell.execute", "shell.inspect"],
            "operations": [
                {
                    "id": "test_pack.run.prepare",
                    "entrypoint_id": "prepare",
                    "implementation_digest": implementation_digest,
                    "effect_ceiling": ["capability:shell.inspect"],
                },
                {
                    "id": "test_pack.run.execute",
                    "entrypoint_id": "execute",
                    "implementation_digest": implementation_digest,
                    "effect_ceiling": ["capability:shell.execute"],
                },
            ],
        }
    )
    record["functions"] = [
        {
            "function_id": "test_pack.run.prepare",
            "contract_id": "tobkiri.service.test.v1",
            "operation_ids": ["test_pack.run.prepare"],
            "implementation_digest": implementation_digest,
        },
        {
            "function_id": "test_pack.run.execute",
            "contract_id": "tobkiri.service.test.v1",
            "operation_ids": ["test_pack.run.execute"],
            "implementation_digest": implementation_digest,
        },
    ]
    return record


def test_migration_source_view_excludes_generated_projection_envelopes(
    tmp_path: Path,
) -> None:
    """Authority metadata must not churn one-way source evidence hashes."""
    ecosystem = tmp_path / "ecosystem.json"
    ecosystem.write_text(
        json.dumps(
            {
                "dependencies": {"workspace": ">=1.0.0"},
                "metadata": {
                    "canonical_v4": {
                        "artifact_digest": "sha256:" + "a" * 64,
                        "source_identity": "sha256:" + "b" * 64,
                    },
                    "format": "rumi.ecosystem.v1",
                    "generated": True,
                    "generated_from": {"source_content_hash": "generated"},
                    "manifest_authority": "v3-authoritative",
                    "owner": "source",
                    "read_only_projection": True,
                },
                "provenance": {
                    "content_hash": "sha256:" + "c" * 64,
                },
                "runtime": {"implementation": "runtime.py"},
            }
        ),
        encoding="utf-8",
    )
    v3 = tmp_path / "rumi.pack.v3.json"
    v3.write_text(
        json.dumps(
            {
                "entrypoints": [
                    {
                        "module": "ecosystem.example.runtime",
                        "artifact_hash": "sha256:" + "d" * 64,
                    }
                ],
                "extensions": {
                    "rumi.legacy_projection": {
                        "manifest": {"provenance": {"content_hash": "generated"}}
                    },
                    "source_extension": {"stable": True},
                },
                "provenance": {"content_hash": "sha256:" + "e" * 64},
            }
        ),
        encoding="utf-8",
    )

    legacy_view = _migration_source_view(ecosystem)
    v3_view = _migration_source_view(v3)

    legacy_payload = json.loads(ecosystem.read_text(encoding="utf-8"))
    legacy_payload["metadata"]["canonical_v4"]["artifact_digest"] = "sha256:" + "f" * 64
    legacy_payload["provenance"]["content_hash"] = "sha256:" + "0" * 64
    ecosystem.write_text(json.dumps(legacy_payload), encoding="utf-8")
    v3_payload = json.loads(v3.read_text(encoding="utf-8"))
    v3_payload["extensions"]["rumi.legacy_projection"]["manifest"] = {
        "provenance": {"content_hash": "changed"}
    }
    v3_payload["provenance"]["content_hash"] = "sha256:" + "1" * 64
    v3.write_text(json.dumps(v3_payload), encoding="utf-8")

    assert _migration_source_view(ecosystem) == legacy_view
    assert _migration_source_view(v3) == v3_view


def test_v3_import_hashes_entrypoint_bytes_not_stale_projection_hashes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A runtime edit must update v4 authority before v3 projection repair."""
    source_root = CATALOG.parents[1] / "ecosystem" / "rumi_provider_adapters_pack"
    pack_root = tmp_path / source_root.name
    pack_root.mkdir()
    for name in ("ecosystem.json", "rumi.pack.v3.json"):
        payload = json.loads((source_root / name).read_text(encoding="utf-8"))
        if name == "rumi.pack.v3.json":
            for entrypoint in payload["entrypoints"]:
                entrypoint["artifact_hash"] = "sha256:" + "0" * 64
        (pack_root / name).write_text(json.dumps(payload), encoding="utf-8")

    monkeypatch.setattr(
        "scripts.migrate_pack_artifacts_v4._runtime_artifacts",
        lambda _pack_root: [],
    )
    monkeypatch.setattr(
        "scripts.migrate_pack_artifacts_v4._source_evidence",
        lambda _pack_root, _paths: [],
    )

    record = _import_record(pack_root)
    expected = _file_digest(source_root / "runtime" / "adapter.py")
    imported = {
        operation["implementation_digest"]
        for contract in record["provided_contracts"]
        for operation in contract["operations"]
    }

    assert imported == {expected}


def test_legacy_import_is_a_draft_and_does_not_promote_or_invent_owners(
    tmp_path: Path,
) -> None:
    """Legacy conversion cannot write v4 authority or pick an owner by order."""
    source_root = CATALOG.parents[1] / "ecosystem" / "rumi_provider_adapters_pack"
    pack_root = tmp_path / source_root.name
    pack_root.mkdir()
    for name in ("ecosystem.json", "rumi.pack.v3.json"):
        (pack_root / name).write_text(
            (source_root / name).read_text(encoding="utf-8"),
            encoding="utf-8",
        )

    record = _import_record(pack_root)
    assert record["authority"] == "migration-draft"
    source_v3 = json.loads((pack_root / "rumi.pack.v3.json").read_text(encoding="utf-8"))
    expected_owners = {
        contract["id"]: contract["lifecycle"]["data_owner"]
        for contract in source_v3["contracts"]["provides"]
        if contract.get("lifecycle", {}).get("data_owner")
    }
    assert {
        contract["contract_id"]: contract["owner"]
        for contract in record["provided_contracts"]
        if "owner" in contract
    } == {
        module_id.replace("rumi.", "tobkiri.", 1): owner
        for module_id, owner in expected_owners.items()
    }

    with pytest.raises(PackV4MigrationError, match="requires --draft-output"):
        import_legacy(check=False)


def test_catalog_validation_does_not_use_a_pack_count_as_authority() -> None:
    """The canonical source set is inventory-driven, not a magic total."""
    payload = copy.deepcopy(_catalog())
    payload["packs"].pop()
    payload["pack_ids"].pop()

    records = _validate_catalog_payload(payload)

    assert len(records) == len(payload["pack_ids"])


def test_all_packs_have_valid_deterministic_v4_artifacts() -> None:
    """Every declared Pack must match a second byte-identical generation."""
    catalog = _catalog()
    pack_count = len(catalog["packs"])
    expected_contracts = sum(len(record["provided_contracts"]) for record in catalog["packs"])
    expected_operations = sum(
        len(contract["operations"])
        for record in catalog["packs"]
        for contract in record["provided_contracts"]
    )
    result = generate(check=True)
    assert result == {
        "packs": pack_count,
        "valid": pack_count,
        "contracts": expected_contracts,
        "operations": expected_operations,
    }
    payload = _catalog()
    assert payload["excluded_packs"] == sorted(EXCLUDED_PACKS)
    for record in payload["packs"]:
        files = _render_record(record)
        verify_rendered_artifacts(files)
        pack_root = CATALOG.parents[1] / "ecosystem" / record["pack_id"]
        assert {name: (pack_root / name).read_text(encoding="utf-8") for name in files} == files


def test_active_shell_policy_pack_is_not_a_read_only_compatibility_projection() -> None:
    """A sandboxed Defaults provider must remain admissible after activation."""
    record = next(
        item
        for item in _catalog()["packs"]
        if item["pack_id"] == "rumi_shell_policy_pack"
    )

    assert record["kind"] == "normal_sandbox"
    assert record["migration"]["compatibility"] == "none"
    manifest = json.loads(_render_record(record)["pack.v4.json"])
    assert manifest["migration"]["compatibility"] == "none"


def test_normal_generation_has_no_v3_or_legacy_authority_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Normal generation may consume only the canonical v4 source catalog."""
    original = Path.read_text

    def guarded_read(path: Path, *args: object, **kwargs: object) -> str:
        if path.name in {"rumi.pack.v3.json", "ecosystem.json"}:
            raise AssertionError(f"legacy authority read: {path}")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded_read)
    assert generate(check=True)["valid"] == len(_catalog()["packs"])


@pytest.mark.parametrize("failure", ["duplicate", "missing", "unknown", "malformed"])
def test_catalog_rejects_inventory_and_shape_failures(failure: str) -> None:
    """Unknown, missing, duplicate, and malformed source records fail closed."""
    payload = copy.deepcopy(_catalog())
    if failure == "duplicate":
        payload["packs"][-1] = copy.deepcopy(payload["packs"][0])
    elif failure == "missing":
        payload["packs"].pop()
    elif failure == "unknown":
        payload["packs"][-1]["pack_id"] = "unknown_pack"
    else:
        payload["packs"][0].pop("network")
    with pytest.raises(PackV4MigrationError):
        _validate_catalog_payload(payload)


def test_tampered_and_stale_generated_artifacts_fail_closed() -> None:
    """Content tampering and stale integrity seals cannot pass artifact checks."""
    record = _catalog()["packs"][0]
    files = _render_record(record)

    tampered = dict(files)
    manifest = json.loads(tampered["pack.v4.json"])
    manifest["pack"]["display_name"] += " tampered"
    tampered["pack.v4.json"] = json.dumps(manifest, sort_keys=True) + "\n"
    with pytest.raises(PackV4MigrationError, match="digest mismatch"):
        verify_rendered_artifacts(tampered)

    stale = dict(files)
    index = json.loads(stale["artifact-index.v4.json"])
    index["integrity_seal"]["signed_digest"] = "sha256:" + "0" * 64
    stale["artifact-index.v4.json"] = json.dumps(index, sort_keys=True) + "\n"
    with pytest.raises(PackV4MigrationError, match="integrity seal"):
        verify_rendered_artifacts(stale)

    malformed = dict(files)
    malformed["pack.v4.json"] = "{}\n"
    with pytest.raises(SchemaValidationError):
        verify_rendered_artifacts(malformed)


def test_global_catalog_has_no_duplicate_provider_or_operation() -> None:
    """Canonical Provider and Operation identities are globally unique."""
    providers: set[str] = set()
    operations: set[str] = set()
    owners: dict[str, str] = {}
    catalog = _catalog()
    expected_provider_count = sum(
        len(record.get("functions", record["provided_contracts"])) for record in catalog["packs"]
    )
    expected_operation_count = sum(
        len(contract["operations"])
        for record in catalog["packs"]
        for contract in record["provided_contracts"]
    )
    for record in catalog["packs"]:
        files = _render_record(record)
        manifest = json.loads(files["pack.v4.json"])
        contracts = json.loads(files["contracts.v4.json"])["contracts"]
        for contract in contracts:
            assert (
                owners.setdefault(contract["contract_id"], contract["owner"]) == contract["owner"]
            )
        for provider in manifest["provider_catalog"]:
            assert provider["provider_id"] not in providers
            providers.add(provider["provider_id"])
        for operation in manifest["operation_catalog"]:
            assert operation["source_kind"] == "canonical_v4_contract"
            assert operation["operation_id"] not in operations
            operations.add(operation["operation_id"])
    assert len(providers) == expected_provider_count
    assert len(operations) == expected_operation_count


def test_legacy_operation_source_cannot_enter_v4_authority_catalog() -> None:
    """Offline legacy evidence is never an executable v4 Operation."""
    record = next(
        item
        for item in _catalog()["packs"]
        if item["pack_id"] == "rumi_agent_continuity_pack"
    )
    files = _render_record(record)
    manifest = json.loads(files["pack.v4.json"])
    assert manifest["functions"] == []
    assert manifest["operation_catalog"] == []
    assert manifest["provider_catalog"] == []

    manifest["operation_catalog"] = [
        {
            "operation_id": "rumi_agent_continuity_pack.legacy.operation",
            "owner": record["pack_id"],
            "source_kind": "legacy_component",
            "effect_ceiling": [],
        }
    ]
    files["pack.v4.json"] = json.dumps(manifest, sort_keys=True) + "\n"
    with pytest.raises(PackV4MigrationError, match="executable catalog entries"):
        verify_rendered_artifacts(files)


def test_legacy_record_rendering_remains_byte_identical() -> None:
    """Omitting explicit Functions keeps the checked-in legacy projection exact."""

    record = next(item for item in _catalog()["packs"] if item["pack_id"] == "defaultspack")
    rendered = _render_record(record)
    pack_root = CATALOG.parents[1] / "ecosystem" / "defaultspack"

    assert {name: (pack_root / name).read_text(encoding="utf-8") for name in rendered} == rendered


def test_explicit_functions_bind_multiple_principals_and_operation_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit Functions partition one Contract without widening effect ceilings."""

    record = _explicit_multi_function_record(tmp_path, monkeypatch)
    rendered = _render_record(record)
    verify_rendered_artifacts(rendered)
    manifest = json.loads(rendered["pack.v4.json"])
    executable = json.loads(rendered["executables.v4.json"])

    assert [item["id"] for item in manifest["functions"]] == [
        "test_pack.run.execute",
        "test_pack.run.prepare",
    ]
    assert {
        item["operation_id"]: item["effect_ceiling"] for item in manifest["operation_catalog"]
    } == {
        "test_pack.run.prepare": ["capability:shell.inspect"],
        "test_pack.run.execute": ["capability:shell.execute"],
    }
    assert {(item["function_id"], item["variant_id"]) for item in executable["variants"]} == {
        ("test_pack.run.prepare", "test_pack.run.prepare.python"),
        ("test_pack.run.execute", "test_pack.run.execute.python"),
    }


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        (
            lambda record: record["functions"].append(copy.deepcopy(record["functions"][0])),
            "Function identity",
        ),
        (
            lambda record: record["functions"][0].update(
                {"operation_ids": ["test_pack.not-declared"]}
            ),
            "Contract-external",
        ),
        (
            lambda record: record["functions"][0].update(
                {"implementation_digest": "sha256:not-a-digest"}
            ),
            "Function identity",
        ),
        (
            lambda record: record["functions"][0].update(
                {"implementation_digest": "sha256:" + "0" * 64}
            ),
            "implementation artifact",
        ),
        (
            lambda record: record["functions"][1].update(
                {"operation_ids": ["test_pack.run.prepare"]}
            ),
            "Operation is duplicated",
        ),
    ),
)
def test_explicit_function_source_rejects_ambiguous_or_invalid_bindings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: object,
    message: str,
) -> None:
    """Ambiguous Function sources never produce a partially valid authority set."""

    record = _explicit_multi_function_record(tmp_path, monkeypatch)
    assert callable(mutation)
    mutation(record)

    with pytest.raises(PackV4MigrationError, match=message):
        _render_record(record)


def test_artifact_index_allows_sealed_host_contribution_classifications(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Launcher-consumed sidecar roles are sealed classifications, not grants."""

    record = _explicit_multi_function_record(tmp_path, monkeypatch)
    record["runtime_artifacts"].extend(
        [
            {
                "path": "host_contract_contributions.v1.json",
                "digest": "sha256:" + "a" * 64,
                "kind": "sidecar",
                "index_role": "host_contract_contribution",
            },
            {
                "path": "update_metadata.v1.json",
                "digest": "sha256:" + "b" * 64,
                "kind": "sidecar",
                "index_role": "host_contract_update_metadata",
            },
        ]
    )

    rendered = _render_record(record)
    index = json.loads(rendered["artifact-index.v4.json"])
    roles = {item["path"]: item["role"] for item in index["artifacts"]}
    assert roles["host_contract_contributions.v1.json"] == "host_contract_contribution"
    assert roles["update_metadata.v1.json"] == "host_contract_update_metadata"
    assert all(
        item["kind"] == "sidecar"
        for item in json.loads(rendered["pack.v4.json"])["artifacts"]
        if item["path"] in roles and item["path"].endswith(".v1.json")
    )


def test_explicit_function_digest_requires_one_runtime_executable_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A digest cannot ambiguously select multiple runtime implementation paths."""

    record = _explicit_multi_function_record(tmp_path, monkeypatch)
    implementation_digest = record["functions"][0]["implementation_digest"]
    record["runtime_artifacts"].append(
        {
            "path": "runtime/duplicate.py",
            "digest": implementation_digest,
            "kind": "executable",
        }
    )

    with pytest.raises(PackV4MigrationError, match="implementation artifact"):
        _render_record(record)
