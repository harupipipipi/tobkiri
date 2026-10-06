"""Canonical input shapes and finite selected edges for project folder admission."""

from pathlib import Path
import hashlib
import json
import pytest
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = "tobkiri.service.workspace.project.v1"
PACK = "rumi_workspace_mount_pack"
COORDINATOR = "rumi_host_authority_bridge_pack.host-authority.interactive-effect"


def load(path):
    return json.loads((ROOT / path).read_text())


def test_canonical_sources_select_only_native_choice_and_interactive_execute():
    profile = load("ecosystem/defaultspack/v4/defaults.profile.intent.v1.json")
    edges = [e for e in profile["requested_edges"] if e["contract_id"] == CONTRACT]
    assert {
        (e["caller_function_id"], e["operation_id"], e["authority_mode"]) for e in edges
    } == {
        ("shell.tauri.default", "workspace.directory.acquire", "profile_grant"),
        (COORDINATOR, "workspace.mount.prepare", "profile_grant"),
        (COORDINATOR, "workspace.mount.execute", "interactive_only"),
    }
    for edge in edges:
        assert edge["requested_scope_template"]["dimensions"] == {
            "contract": [CONTRACT],
            "operation": [edge["operation_id"]],
        }
    routes = load("ecosystem/defaultspack/defaultspack/frontend_contract_map.v4.json")[
        "routes"
    ]
    picker = next(r for r in routes if r["path"] == "/api/ui/select-directory")
    assert picker["targets"][0]["allowed_payload_keys"] == []
    assert picker["targets"][0]["contract_id"] == CONTRACT
    mount = next(r for r in routes if r["path"] == "/api/projects/workspace")
    assert mount["targets"][0]["function_id"] == COORDINATOR
    assert "root_path" not in mount["targets"][0]["allowed_payload_keys"]


@pytest.mark.parametrize(
    "payload",
    [
        {"path": "/arbitrary"},
        {"approved": True},
        {"profile_id": "other"},
        {"prompt": "browser text"},
    ],
)
def test_picker_schema_rejects_all_browser_supplied_authority(payload):
    entries = load("tests/fixtures/legacy_executable_sources.v1.json")["packs"][PACK][
        "entries"
    ]
    schema = next(
        e for e in entries if e["operation_ids"] == ["workspace.directory.acquire"]
    )["schemas"]["input"]
    assert list(Draft202012Validator(schema).iter_errors(payload))
    assert not list(Draft202012Validator(schema).iter_errors({}))


@pytest.mark.parametrize(
    "payload",
    [
        {"root_path": "/arbitrary"},
        {"selection_id": "ticket", "approved": True},
        {"selection_id": "ticket", "profile_id": "foreign"},
    ],
)
def test_prepare_schema_accepts_only_an_opaque_ticket(payload):
    entries = load("tests/fixtures/legacy_executable_sources.v1.json")["packs"][PACK][
        "entries"
    ]
    schema = next(
        e for e in entries if e["operation_ids"] == ["workspace.mount.prepare"]
    )["schemas"]["input"]
    assert list(Draft202012Validator(schema).iter_errors(payload))
    assert not list(
        Draft202012Validator(schema).iter_errors({"selection_id": "opaque-ticket"})
    )


def test_pending_effect_schema_accepts_exact_mount_request_without_approval_claims():
    fixture = load("tests/fixtures/legacy_executable_sources.v1.json")
    schema = next(
        e
        for e in fixture["packs"]["rumi_host_authority_bridge_pack"]["entries"]
        if e["function_id"] == COORDINATOR
    )["schemas"]["input"]
    payload = {
        "phase": "prepare",
        "effect_kind": "workspace_mount",
        "request": {"selection_id": "ticket"},
        "correlation_id": "00000000-0000-4000-8000-000000000001",
    }
    validator = Draft202012Validator(schema)
    assert not list(validator.iter_errors(payload))
    assert list(
        validator.iter_errors(
            {**payload, "request": {"selection_id": "ticket", "approved": True}}
        )
    )
    assert list(
        validator.iter_errors({**payload, "request": {"root_path": "/arbitrary"}})
    )


def test_new_hook_implementation_is_digest_bound_with_existing_workspace_owner():
    catalog = load("schemas/pack_v4_catalog.v1.json")
    record = next(p for p in catalog["packs"] if p["pack_id"] == PACK)
    digest = (
        "sha256:"
        + hashlib.sha256(
            (
                ROOT / "ecosystem/rumi_workspace_mount_pack/runtime/mounts.py"
            ).read_bytes()
        ).hexdigest()
    )
    assert {
        a["digest"]
        for a in record["runtime_artifacts"]
        if a["path"] == "runtime/mounts.py"
    } == {digest}
    contract = next(
        c for c in record["provided_contracts"] if c["contract_id"] == CONTRACT
    )
    assert {o["id"] for o in contract["operations"]} == {
        "workspace.directory.acquire",
        "workspace.mount.prepare",
        "workspace.mount.execute",
    }
    assert {o["implementation_digest"] for o in contract["operations"]} == {digest}


def test_production_migration_projects_three_distinct_finite_factory_functions():
    # This reads the production projection without regenerating any artifact.
    from scripts.migrate_pack_artifacts_v4 import _function_sources

    record = next(
        p
        for p in load("schemas/pack_v4_catalog.v1.json")["packs"]
        if p["pack_id"] == PACK
    )
    projected = {
        (f["function_id"], tuple(f["operation_ids"])) for f in _function_sources(record)
    }
    assert projected == {
        (PACK + ".workspace-mount.manage", (PACK + ".workspace-mount",)),
        (PACK + ".workspace-mount.resource", (PACK + ".workspace-resource",)),
        (PACK + ".project-directory.service", ("workspace.directory.acquire",)),
        (PACK + ".project-workspace-prepare.service", ("workspace.mount.prepare",)),
        (PACK + ".project-workspace-execute.service", ("workspace.mount.execute",)),
    }
