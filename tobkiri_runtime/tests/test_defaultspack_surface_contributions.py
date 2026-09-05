from __future__ import annotations

from core_runtime.surface_launch_target import extract_surface_launch_target
from ecosystem.defaultspack.capability_bindings import bind_frontend_surface
from ecosystem.defaultspack.defaultspack.surface_contributions import (
    defaultspack_computer_artifact_destination,
    defaultspack_web_mounts,
    migrate_defaultspack_frontend_surface_contributions,
)


def test_legacy_frontend_target_migrates_to_generic_surface_contribution():
    profile = {
        "defaultspack": {
            "frontends": {
                "frontend-1": {
                    "surface_launch_target": {
                        "kind": "desktop_app",
                        "pack_id": "defaultspack",
                        "principal_id": "defaultspack",
                        "surface_source": "profile",
                        "env": {
                            "DEFAULTSPACK_CUSTOM": "preserve",
                            "RUMI_DEFAULTSPACK_SURFACE": "browser",
                        },
                    }
                }
            }
        }
    }

    assert migrate_defaultspack_frontend_surface_contributions(profile) == 1
    contribution = profile["surface_contributions"][0]
    target = extract_surface_launch_target(
        profile,
        fallback_pack_id=None,
        surfaces={"preferred": "desktop", "enabled": ["desktop"]},
    )

    assert contribution["owner_pack_id"] == "defaultspack"
    assert "RUMI_DEFAULTSPACK_SURFACE" not in contribution["target"]["env"]
    assert target is not None
    assert target["env"] == {
        "RUMI_PROFILE_SURFACE": "desktop",
        "RUMI_DEFAULTSPACK_OPEN_BROWSER": "1",
        "RUMI_DEFAULTSPACK_SURFACE": "webview",
        "DEFAULTSPACK_CUSTOM": "preserve",
    }


def test_defaultspack_web_mounts_keep_the_pack_owned_legacy_alias(tmp_path):
    mounts = defaultspack_web_mounts(tmp_path / "defaultspack")

    assert {mount["path_prefix"] for mount in mounts} == {
        "/chat",
        "/static",
        "/desktops",
    }
    desktops = next(mount for mount in mounts if mount["path_prefix"] == "/desktops")
    assert desktops["web_root"] == (tmp_path / "defaultspack" / "ui").resolve()
    assert desktops["auth_required"] is True
    assert desktops["auth_bootstrap"] is True
    static = next(mount for mount in mounts if mount["path_prefix"] == "/static")
    assert static["auth_bootstrap"] is False


def test_defaultspack_computer_destination_is_an_explicit_pack_contribution(
    tmp_path,
):
    contribution = defaultspack_computer_artifact_destination(
        tmp_path / "chat" / "conversations.json"
    )

    assert contribution == {
        "schema": "io.tobkiri.computer-artifact-destination.v1",
        "kind": "conversation_workspace",
        "root": str((tmp_path / "chat" / "conversations").resolve()),
    }


def test_frontend_binding_publishes_defaultspack_surface_contribution():
    source = type(
        "Source",
        (),
        {"id": "surface-instance", "ref": "defaultspack.web_surface"},
    )()
    frontend = type(
        "Frontend",
        (),
        {"id": "frontend-instance", "ref": "defaultspack.frontend"},
    )()
    profile: dict[str, object] = {}

    assert (
        bind_frontend_surface(
            profile,
            source,
            frontend,
            nodes={
                "defaultspack.web_surface": {
                    "metadata": {
                        "pack_id": "defaultspack",
                        "launch": {"kind": "desktop_app"},
                    }
                }
            },
        )
        is None
    )
    target = extract_surface_launch_target(
        profile,
        fallback_pack_id=None,
        surfaces={"preferred": "desktop", "enabled": ["desktop"]},
    )

    assert profile["surface_contributions"][0]["owner_pack_id"] == "defaultspack"
    assert target is not None
    assert target["env"]["RUMI_DEFAULTSPACK_SURFACE"] == "webview"
