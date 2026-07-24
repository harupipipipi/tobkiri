from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
import yaml

from core_runtime.ecosystem_nodes import EcosystemNodeRegistry
from core_runtime.interface_registry import InterfaceRegistry
from core_runtime.node_models import NodeDefinition
from core_runtime.profile_loader import CapabilityProfileLoader, ProfileDiscoveryError
from core_runtime.profile_models import ProfileValidationError, load_profile_document
from core_runtime.profile_node_registry import ProfileNodeRegistry


class FakeApprovalManager:
    def __init__(self, approved: set[str] | None = None) -> None:
        self.approved = approved or set()

    def is_pack_approved_and_verified(self, pack_id: str):
        if pack_id in self.approved:
            return True, None
        return False, "not_approved"


def _pack(tmp_path, pack_id: str, files: dict[str, dict]) -> SimpleNamespace:
    pack_dir = tmp_path / pack_id
    pack_dir.mkdir()
    (pack_dir / "ecosystem.json").write_text(
        json.dumps(
            {
                "pack_id": pack_id,
                "pack_identity": f"test:{pack_id}",
                "version": "1.0.0",
            }
        ),
        encoding="utf-8",
    )
    for rel_path, payload in files.items():
        path = pack_dir / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        if rel_path.endswith(".json"):
            path.write_text(json.dumps(payload), encoding="utf-8")
        else:
            path.write_text(yaml.safe_dump(payload, allow_unicode=True), encoding="utf-8")
    return SimpleNamespace(pack_id=pack_id, subdir=pack_dir, path=pack_dir)


def _registry(*packs: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(packs={pack.pack_id: pack for pack in packs})


def _profile_doc(profile_id: str, **overrides) -> dict:
    data = {
        "profile_id": profile_id,
        "version": "rumi.profile.v1",
        "kind": "runtime_profile",
        "locale": "ja",
        "display_name": {"en": profile_id},
        "permissions": {"can_install_packs": False},
        "enabled_nodes": ["samplepack.agent"],
        "disabled_nodes": [],
        "node_settings": {},
        "policy": {"write_actions_require_approval": True},
    }
    data.update(overrides)
    return data


def _node_doc(*nodes: dict) -> dict:
    return {"version": "rumi.node.v1", "nodes": list(nodes)}


def test_profile_fields_are_normalized() -> None:
    profile = load_profile_document(
        _profile_doc(
            "coding",
            name="Coding",
            display_name={},
            node_settings={"samplepack.agent": {"credential_ref": "local.key"}},
        )
    )

    assert profile.display_name["en"] == "Coding"
    assert profile.locale == "ja"
    assert profile.permissions["can_install_packs"] is False
    assert profile.node_settings["samplepack.agent"]["credential_ref"] == "local.key"


def test_invalid_profile_version_is_rejected() -> None:
    with pytest.raises(ProfileValidationError, match="unsupported profile version"):
        load_profile_document({"profile_id": "bad", "version": "rumi.profile.v0"})


def test_pack_profiles_load_only_from_approved_packs(tmp_path) -> None:
    approved = _pack(
        tmp_path,
        "approvedpack",
        {"profiles/coding.profile.yaml": _profile_doc("approvedpack.coding")},
    )
    unapproved = _pack(
        tmp_path,
        "unapprovedpack",
        {"profiles/guest.profile.yaml": _profile_doc("unapprovedpack.guest")},
    )
    interface_registry = InterfaceRegistry()
    loader = CapabilityProfileLoader(
        registry=_registry(approved, unapproved),
        approval_manager=FakeApprovalManager({"approvedpack"}),
        interface_registry=interface_registry,
        shared_profiles_dir=tmp_path / "missing-user-profiles",
    )

    profiles = loader.load_all_profiles()

    assert set(profiles) == {"approvedpack.coding"}
    assert loader.diagnostics[0]["code"] == "pack_skipped_unapproved"
    assert interface_registry.get("profile.approvedpack.coding").profile_id == "approvedpack.coding"


def test_user_shared_profiles_are_schema_validated(tmp_path) -> None:
    shared_dir = tmp_path / "user_data" / "shared" / "profiles"
    shared_dir.mkdir(parents=True)
    (shared_dir / "broken.profile.yaml").write_text(
        yaml.safe_dump({"profile_id": "broken", "version": "wrong"}),
        encoding="utf-8",
    )
    loader = CapabilityProfileLoader(
        registry=_registry(),
        approval_manager=FakeApprovalManager(),
        shared_profiles_dir=shared_dir,
    )

    with pytest.raises(ProfileDiscoveryError):
        loader.load_all_profiles()

    assert loader.diagnostics[0]["code"] == "invalid_profile_file"


def test_invalid_profile_can_be_isolated_for_read_only_catalogs(tmp_path) -> None:
    shared_dir = tmp_path / "user_data" / "shared" / "profiles"
    shared_dir.mkdir(parents=True)
    (shared_dir / "broken.profile.yaml").write_text(
        yaml.safe_dump({"profile_id": "broken", "version": "wrong"}),
        encoding="utf-8",
    )
    loader = CapabilityProfileLoader(
        registry=_registry(),
        approval_manager=FakeApprovalManager(),
        shared_profiles_dir=shared_dir,
        continue_on_invalid=True,
    )

    assert loader.load_all_profiles(register=False) == {}
    assert loader.diagnostics[0]["code"] == "invalid_profile_file"


def test_user_shared_profile_overrides_pack_profile(tmp_path) -> None:
    shared_dir = tmp_path / "profiles"
    shared_dir.mkdir()
    (shared_dir / "coding.profile.yaml").write_text(
        yaml.safe_dump(_profile_doc("coding", display_name={"en": "User Coding"})),
        encoding="utf-8",
    )
    pack = _pack(
        tmp_path,
        "samplepack",
        {"profiles/coding.profile.yaml": _profile_doc("coding", display_name={"en": "Pack Coding"})},
    )
    loader = CapabilityProfileLoader(
        registry=_registry(pack),
        approval_manager=FakeApprovalManager({"samplepack"}),
        shared_profiles_dir=shared_dir,
    )

    profiles = loader.load_all_profiles()

    assert profiles["coding"].display_name["en"] == "User Coding"
    assert loader.diagnostics[0]["code"] == "profile_skipped_user_override"


def test_duplicate_user_shared_profile_ids_are_rejected(tmp_path) -> None:
    shared_dir = tmp_path / "profiles"
    shared_dir.mkdir()
    (shared_dir / "a.profile.yaml").write_text(
        yaml.safe_dump(_profile_doc("coding", display_name={"en": "Coding A"})),
        encoding="utf-8",
    )
    (shared_dir / "b.profile.yaml").write_text(
        yaml.safe_dump(_profile_doc("coding", display_name={"en": "Coding B"})),
        encoding="utf-8",
    )
    loader = CapabilityProfileLoader(
        registry=_registry(),
        approval_manager=FakeApprovalManager(),
        shared_profiles_dir=shared_dir,
    )

    with pytest.raises(ProfileDiscoveryError, match="duplicate profile_id 'coding'"):
        loader.load_all_profiles()


def test_profile_node_registry_computes_enabled_and_missing_state(tmp_path) -> None:
    pack = _pack(
        tmp_path,
        "samplepack",
        {
            "components/agent/node.json": _node_doc(
                {
                    "node_id": "samplepack.agent",
                    "ports": [],
                    "requirements": {"required_settings": ["api_key"]},
                }
            ),
            "components/tool/node.json": _node_doc({"node_id": "samplepack.tool", "ports": []}),
        },
    )
    node_registry = EcosystemNodeRegistry(
        registry=_registry(pack),
        approval_manager=FakeApprovalManager({"samplepack"}),
    )
    node_registry.load_all_nodes(register=False)
    profile = load_profile_document(
        _profile_doc(
            "coding",
            enabled_nodes=["samplepack.agent", "missingpack.node"],
            disabled_nodes=["samplepack.tool"],
        )
    )

    profile_nodes = ProfileNodeRegistry(node_registry=node_registry, profile=profile)
    agent_state = profile_nodes.node_state("samplepack.agent")
    tool_state = profile_nodes.node_state("samplepack.tool")
    missing_state = profile_nodes.node_state("missingpack.node")

    assert isinstance(node_registry.get_node("samplepack.agent"), NodeDefinition)
    assert agent_state["enabled"] is True
    assert agent_state["configured"] is False
    assert agent_state["missing"] == ["api_key"]
    assert tool_state["status"] == "disabled"
    assert missing_state["status"] == "missing_node"
