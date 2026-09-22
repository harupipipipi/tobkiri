"""First-start Defaults v4 confirmation and restart transaction tests."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from core_runtime.authority.v4 import AuthorityStore
from core_runtime.bootstrap import profile_capture
from ecosystem.defaultspack.domain.runtime_v4 import (
    BundleIntegrityError,
    ProfileResolutionDenied,
)
from tobkiri_protocol.canonical import canonical_json


def _tree_digests(root: Path) -> dict[str, str]:
    if not root.exists():
        return {}
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*")
        if path.is_file()
    }


def test_review_and_cancel_are_read_only(tmp_path: Path, monkeypatch) -> None:
    user_data = tmp_path / "user-data"
    monkeypatch.setenv("TOBKIRI_USER_DATA", str(user_data))

    confirmation = profile_capture.prepare_default_profile_confirmation()

    assert confirmation["profile_id"] == "defaults"
    assert confirmation["base"]["pack_id"] == "defaults-basepack"
    assert confirmation["shell"]["provider_id"] == "shell.tauri.default"
    catalog = profile_capture.BundledCatalog.load(profile_capture._bundle_root())
    variant = catalog.shells["shell.tauri.default"]["launch"]["variants"][0]
    assert confirmation["shell"]["executable_artifact_digest"] == variant[
        "entrypoint_digest"
    ]
    selected = {
        confirmation["base"]["pack_id"],
        confirmation["shell"]["pack_id"],
        *(binding["pack_id"] for binding in confirmation["bindings"]),
    }
    assert "shell.cli.default" not in selected
    assert "dev.tauri.toolchain.default" not in selected
    assert not user_data.exists()


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("catalog_revision", "sha256:" + "0" * 64),
        ("profile_revision", "sha256:" + "1" * 64),
        (
            "shell",
            {
                "provider_id": "shell.tauri.default",
                "pack_id": "shell.tauri.default",
                "artifact_digest": "sha256:" + "6" * 64,
                "executable_artifact_digest": "sha256:" + "0" * 64,
                "contract_id": "app.shell.v1",
                "definition_digest": "sha256:" + "8" * 64,
            },
        ),
        ("security_epoch", 9),
        ("bindings", []),
    ],
)
def test_tamper_and_provider_mismatch_write_nothing(
    tmp_path: Path,
    monkeypatch,
    field: str,
    replacement: object,
) -> None:
    user_data = tmp_path / "user-data"
    monkeypatch.setenv("TOBKIRI_USER_DATA", str(user_data))
    confirmation = profile_capture.prepare_default_profile_confirmation()
    confirmation[field] = replacement

    with pytest.raises(ProfileResolutionDenied, match="stale or tampered"):
        profile_capture.capture_default_profile(confirmation=confirmation)

    assert not user_data.exists()


def test_stale_catalog_shell_executable_pin_is_rejected(
    tmp_path: Path, monkeypatch
) -> None:
    source = profile_capture._bundle_root()  # noqa: SLF001 - integrity fixture
    bundle = tmp_path / "bundle"
    shutil.copytree(source, bundle)
    shutil.copytree(
        source.parent / "platform-artifacts",
        bundle.parent / "platform-artifacts",
    )

    profile_path = bundle / "defaults.profile.v4.json"
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    profile["shell"]["executable_artifact_digest"] = "sha256:" + "0" * 64
    profile_bytes = canonical_json(profile) + b"\n"
    profile_path.write_bytes(profile_bytes)

    lock_path = bundle / "bundle.lock.json"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    profile_entry = next(
        entry
        for entry in lock["entries"]
        if entry["path"] == "defaults.profile.v4.json"
    )
    profile_entry["digest"] = "sha256:" + hashlib.sha256(profile_bytes).hexdigest()
    lock_path.write_bytes(canonical_json(lock) + b"\n")

    monkeypatch.setattr(profile_capture, "_bundle_root", lambda _base=None: bundle)
    with pytest.raises(ProfileResolutionDenied, match="Profile Shell executable pin"):
        profile_capture.prepare_default_profile_confirmation()


def test_commit_restart_and_replay_are_finite(tmp_path: Path, monkeypatch) -> None:
    user_data = tmp_path / "user-data"
    monkeypatch.setenv("TOBKIRI_USER_DATA", str(user_data))
    confirmation = profile_capture.prepare_default_profile_confirmation()

    active = profile_capture.capture_default_profile(confirmation=confirmation)
    before_replay = _tree_digests(user_data)
    restarted = profile_capture.capture_default_profile()
    assert restarted.activation == active.activation
    with pytest.raises(ProfileResolutionDenied, match="replayed"):
        profile_capture.capture_default_profile(confirmation=confirmation)
    assert _tree_digests(user_data) == before_replay
    assert not (user_data / "settings" / "startup_profiles.json").exists()
    assert not (user_data / "settings" / "setup_pack_selection.json").exists()


def test_stale_security_epoch_cannot_create_profile_state(
    tmp_path: Path, monkeypatch
) -> None:
    user_data = tmp_path / "user-data"
    monkeypatch.setenv("TOBKIRI_USER_DATA", str(user_data))
    confirmation = profile_capture.prepare_default_profile_confirmation()
    authority = AuthorityStore(user_data / "authority" / "v4.sqlite3")
    authority.advance_security_epoch("test-stale-confirmation")

    with pytest.raises(ProfileResolutionDenied, match="stale or tampered"):
        profile_capture.capture_default_profile(confirmation=confirmation)

    assert not (user_data / "profiles").exists()
    assert not (user_data / "workspaces").exists()


def test_catalog_byte_tamper_is_rejected_before_user_state(
    tmp_path: Path, monkeypatch
) -> None:
    user_data = tmp_path / "user-data"
    bundle = tmp_path / "bundle"
    source = profile_capture._bundle_root()  # noqa: SLF001 - integrity fixture
    shutil.copytree(source, bundle)
    manifest = bundle / "defaults.profile.v4.json"
    manifest.write_bytes(manifest.read_bytes() + b"\n")
    monkeypatch.setenv("TOBKIRI_USER_DATA", str(user_data))
    monkeypatch.setattr(profile_capture, "_bundle_root", lambda _base=None: bundle)

    with pytest.raises(BundleIntegrityError, match="digest changed"):
        profile_capture.prepare_default_profile_confirmation()

    assert not user_data.exists()
