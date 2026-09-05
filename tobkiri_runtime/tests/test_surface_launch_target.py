from __future__ import annotations

from core_runtime.graph_models import GraphNodeInstance
from core_runtime.node_models import load_node_document
from core_runtime.surface_launch_target import (
    extract_surface_launch_target,
    normalize_surface_launch_target,
    surface_env,
    surface_launch_target_from_instance,
)


def _node(payload: dict):
    return load_node_document({"version": "rumi.node.v1", "nodes": [payload]})[0]


def test_surface_launch_target_rejects_cross_pack_launch_metadata():
    node = _node(
        {
            "node_id": "frontendpack.web_surface",
            "ports": [{"id": "surface", "direction": "output", "standards": ["rumi.surface"]}],
            "metadata": {
                "pack_id": "frontendpack",
                "launch": {
                    "kind": "desktop_app",
                    "pack_id": "otherpack",
                },
            },
        }
    )
    diagnostics = []

    target = surface_launch_target_from_instance(
        runtime_profile={},
        instance=GraphNodeInstance(id="frontendpack_web_surface", ref="frontendpack.web_surface"),
        nodes={node.node_id: node},
        diagnostics=diagnostics,
    )

    assert target is None
    assert diagnostics[0]["code"] == "launch_pack_mismatch"


def test_extract_surface_launch_target_falls_back_to_base_pack():
    target = extract_surface_launch_target(
        {"version": "rumi.runtime_profile.v1"},
        fallback_pack_id="defaultspack",
        surfaces={"preferred": "browser", "enabled": ["browser"]},
    )

    assert target is not None
    assert target["pack_id"] == "defaultspack"
    assert target["source"] == "startup_profile_fallback"


def test_normalize_surface_launch_target_rejects_principal_mismatch():
    target = normalize_surface_launch_target(
        {
            "kind": "desktop_app",
            "pack_id": "frontendpack",
            "principal_id": "otherpack",
        },
        fallback_pack_id=None,
    )

    assert target is None


def test_profile_surface_targets_re_resolve_startup_surface():
    node = _node(
        {
            "node_id": "frontendpack.web_surface",
            "ports": [{"id": "surface", "direction": "output", "standards": ["rumi.surface"]}],
            "metadata": {
                "pack_id": "frontendpack",
                "launch": {
                    "kind": "desktop_app",
                    "pack_id": "frontendpack",
                    "env": {"FRONTENDPACK_SURFACE": "web"},
                },
            },
        }
    )

    target = surface_launch_target_from_instance(
        runtime_profile={},
        instance=GraphNodeInstance(id="frontendpack_web_surface", ref="frontendpack.web_surface"),
        nodes={node.node_id: node},
        surfaces={"preferred": "desktop", "enabled": ["desktop"]},
    )
    normalized = normalize_surface_launch_target(
        target,
        fallback_pack_id=None,
        surfaces={"preferred": "browser", "enabled": ["browser"]},
    )

    assert target is not None
    assert target["surface"] == "desktop"
    assert normalized is not None
    assert normalized["surface"] == "browser"
    assert normalized["env"]["RUMI_PROFILE_SURFACE"] == "browser"
    assert normalized["env"]["FRONTENDPACK_SURFACE"] == "web"


def test_surface_contribution_requires_matching_owner_and_target_pack():
    profile = {
        "surface_contributions": [
            {
                "schema": "io.tobkiri.surface-contribution.v1",
                "kind": "surface_launch_target",
                "owner_pack_id": "frontendpack",
                "target": {
                    "kind": "desktop_app",
                    "pack_id": "frontendpack",
                    "principal_id": "frontendpack",
                    "surface_source": "profile",
                    "env_by_surface": {
                        "browser": {"FRONTENDPACK_SURFACE": "web"},
                        "desktop": {"FRONTENDPACK_SURFACE": "native"},
                    },
                },
            },
            {
                "schema": "io.tobkiri.surface-contribution.v1",
                "kind": "surface_launch_target",
                "owner_pack_id": "unrelatedpack",
                "target": {
                    "kind": "desktop_app",
                    "pack_id": "otherpack",
                    "principal_id": "otherpack",
                },
            },
        ]
    }

    target = extract_surface_launch_target(
        profile,
        fallback_pack_id=None,
        surfaces={"preferred": "desktop", "enabled": ["desktop"]},
    )

    assert target is not None
    assert target["pack_id"] == "frontendpack"
    assert target["surface"] == "desktop"
    assert target["env"] == {
        "RUMI_PROFILE_SURFACE": "desktop",
        "FRONTENDPACK_SURFACE": "native",
    }
    assert (
        extract_surface_launch_target(
            {"surface_contributions": [profile["surface_contributions"][1]]},
            fallback_pack_id=None,
        )
        is None
    )


def test_core_surface_environment_does_not_choose_a_pack_runtime():
    assert surface_env("desktop") == {"RUMI_PROFILE_SURFACE": "desktop"}
