from __future__ import annotations

import json
import importlib
import os
import sys
from types import SimpleNamespace
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path

import pytest

from core_runtime.resolved_profile import (
    _pack_content_hash,
    resolution_input_from_startup_profile,
)
from core_runtime.resolved_profile_scope import (
    effective_profile_projections,
    invalidate_persisted_resolved_profile,
    persisted_resolved_profile,
)
from scripts.quality.legacy_selection_migration import (
    apply_legacy_selection_migration,
    plan_legacy_selection_migration,
    rollback_legacy_selection_migration,
)
from tests.conformance_support.packaged_profile import load_packaged_profile_catalog


_DEFAULTSPACK_ROOT = Path(__file__).resolve().parent.parent / "ecosystem" / "defaultspack"
if str(_DEFAULTSPACK_ROOT) not in sys.path:
    sys.path.insert(0, str(_DEFAULTSPACK_ROOT))


@dataclass(frozen=True)
class _CapturedV4Profile:
    """Small test-owned view of one verified protocol Profile graph."""

    resolved: object
    profile_id: str
    effective_pack_set: tuple[str, ...]
    plan_hash: str

    @property
    def profile(self):
        return self.resolved.profile

    @property
    def lock(self):
        return self.resolved.lock

    @property
    def plan(self):
        return self.resolved.plan


@pytest.fixture(autouse=True)
def _isolate_profile_resolution_from_pack_install_policy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    request: pytest.FixtureRequest,
) -> None:
    """Keep resolver tests off user state and synchronize both import aliases.

    Some runtime tests import ``core_runtime`` while others use the
    ``tobkiri_runtime.core_runtime`` compatibility alias.  Without resetting
    both module copies, a preceding startup-profile test can leave the real
    Defaults Profile cache or approval path in this module, making trust and
    invalidation assertions order-dependent.
    """

    for module_name in (
        "core_runtime.resolved_profile_scope",
        "tobkiri_runtime.core_runtime.resolved_profile_scope",
    ):
        try:
            scope_module = importlib.import_module(module_name)
        except ModuleNotFoundError:
            continue
        monkeypatch.setattr(scope_module, "_PERSISTED_PROFILE_CACHE", None)
        monkeypatch.setattr(scope_module, "_PERSISTED_PROFILE_INVALIDATION_REVISION", 0)
        # A preceding startup-profile test can leave a ContextVar-bound plan
        # active.  Clear it before and after each resolver test so persisted
        # recovery actually exercises the isolated temporary state.
        scope_module._ACTIVE_PROFILE.set(None)
        request.addfinalizer(lambda module=scope_module: module._ACTIVE_PROFILE.set(None))

@pytest.fixture
def captured_v4_profile(request: pytest.FixtureRequest):
    """Bind the checked-in, Host-verified Defaults Profile snapshot."""
    from ecosystem.defaultspack.domain.runtime_v4 import resolve_default_profile
    from core_runtime.resolved_profile_scope import (
        activate_resolved_profile,
        restore_resolved_profile,
    )

    catalog = load_packaged_profile_catalog()
    source = catalog.profiles["defaults"]
    authority_bindings = {
        "|".join(
            str(edge[field])
            for field in (
                "caller_function_id",
                "target_provider_id",
                "contract_id",
                "operation_id",
            )
        ): f"authority-ref:test.default.{index}"
        for index, edge in enumerate(source["requested_edges"])
    }
    resolved = resolve_default_profile(
        catalog,
        "defaults",
        approved_artifact_digests={
            str(manifest["pack"]["artifact_digest"])
            for manifest in catalog.packs.values()
        },
        authority_snapshot_digest="sha256:" + "9" * 64,
        authority_bindings=authority_bindings,
        security_epoch=1,
    )
    snapshot = _CapturedV4Profile(
        resolved=resolved,
        profile_id=str(resolved.profile["profile_id"]),
        effective_pack_set=tuple(
            item["identity"] for item in resolved.lock["effective_set"]
        ),
        plan_hash=str(resolved.plan["plan_digest"]),
    )
    token = activate_resolved_profile(snapshot)
    request.addfinalizer(lambda: restore_resolved_profile(token))
    return snapshot


def test_pack_content_hash_does_not_follow_projection_symlinks(tmp_path: Path) -> None:
    pack_root = tmp_path / "defaultspack"
    tools_root = pack_root / "tools"
    external_root = tmp_path / "external"
    tools_root.mkdir(parents=True)
    external_root.mkdir()
    (external_root / "outside.py").write_text("OUTSIDE = 1\n", encoding="utf-8")
    (pack_root / "ecosystem.json").write_text(
        json.dumps(
            {
                "components": {
                    "external": {"path": str(external_root)},
                }
            }
        ),
        encoding="utf-8",
    )
    (tools_root / "local.py").write_text("LOCAL = 1\n", encoding="utf-8")
    (tools_root / "linked").symlink_to(external_root, target_is_directory=True)

    first = _pack_content_hash(pack_root, "manifest")
    (external_root / "outside.py").write_text("OUTSIDE = 2\n", encoding="utf-8")

    assert _pack_content_hash(pack_root, "manifest") == first


def test_pack_content_hash_cache_reuses_only_unchanged_projection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import core_runtime.resolved_profile as resolver

    pack_root = tmp_path / "pack"
    tools_root = pack_root / "tools"
    tools_root.mkdir(parents=True)
    (pack_root / "ecosystem.json").write_text("{}", encoding="utf-8")
    tool_path = tools_root / "tool.json"
    tool_path.write_text('{"version": 1}', encoding="utf-8")
    calls = 0
    original_sha256 = resolver._sha256

    def counted_sha256(path: Path) -> str:
        nonlocal calls
        calls += 1
        return original_sha256(path)

    monkeypatch.setattr(resolver, "_sha256", counted_sha256)
    first = resolver._pack_content_hash(pack_root, "manifest")
    second = resolver._pack_content_hash(pack_root, "manifest")
    assert second == first
    assert calls == 1

    original_stat = tool_path.stat()
    tool_path.write_text('{"version": 2}', encoding="utf-8")
    os.utime(
        tool_path,
        ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
    )
    changed = resolver._pack_content_hash(pack_root, "manifest")

    assert changed != first
    assert calls == 2


def test_pack_content_hash_ignores_runtime_bytecode_but_tracks_source(
    tmp_path: Path,
) -> None:
    """Interpreter caches must not change an installed pack's identity."""
    pack_root = tmp_path / "pack"
    component_root = pack_root / "blocks" / "chat"
    component_root.mkdir(parents=True)
    (pack_root / "ecosystem.json").write_text(
        json.dumps(
            {
                "components": {
                    "chat": {"path": "blocks/chat"},
                }
            }
        ),
        encoding="utf-8",
    )
    source_path = component_root / "create_conversation.py"
    source_path.write_text("VALUE = 1\n", encoding="utf-8")

    original = _pack_content_hash(pack_root, "manifest")
    cache_root = component_root / "__pycache__"
    cache_root.mkdir()
    (cache_root / "create_conversation.cpython-313.pyc").write_bytes(
        b"runtime bytecode"
    )
    (component_root / "create_conversation.pyc").write_bytes(
        b"legacy runtime bytecode"
    )

    assert _pack_content_hash(pack_root, "manifest") == original

    source_path.write_text("VALUE = 2\n", encoding="utf-8")

    assert _pack_content_hash(pack_root, "manifest") != original


def test_startup_profile_input_accepts_only_host_supplied_verified_trust() -> None:
    resolution_input = resolution_input_from_startup_profile(
        {
            "profile_id": "fixture",
            "base_pack": "defaultspack",
            "packs": ["frontendpack"],
        },
        verified_pack_trust={
            "frontendpack": "verified",
            "defaultspack": "system",
        },
    )

    assert resolution_input.verified_pack_trust == (
        ("defaultspack", "system"),
        ("frontendpack", "verified"),
    )


def _fake_v4_activation(activation_id: str = "activation:defaults-test") -> object:
    artifact = "sha256:" + "1" * 64
    return SimpleNamespace(
        activation={"activation_id": activation_id},
        resolved=SimpleNamespace(
            profile={"profile_id": "defaults"},
            lock={
                "effective_set": [
                    {"identity": "defaultspack", "artifact_digest": artifact}
                ]
            },
            plan={
                "profile_revision": "sha256:" + "2" * 64,
                "plan_digest": "sha256:" + "3" * 64,
                "bindings": [],
            },
        ),
    )


def test_persisted_profile_uses_only_committed_v4_activation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import core_runtime.resolved_profile_scope as scope
    import core_runtime.bootstrap.profile_capture as capture_module

    settings_dir = tmp_path / "settings"
    settings_dir.mkdir()
    (settings_dir / "startup_profiles.json").write_text(
        json.dumps(
            {
                "active_profile_id": "fixture",
                "profiles": [
                    {
                        "profile_id": "fixture",
                        "base_pack": "defaultspack",
                        "packs": [],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(scope, "_PERSISTED_PROFILE_CACHE", None)
    monkeypatch.setattr(capture_module, "capture_active_profile", _fake_v4_activation)

    plan = persisted_resolved_profile()

    assert plan is not None
    assert plan.profile_id == "defaults"
    assert plan.effective_pack_set == ("defaultspack",)
    assert plan.plan_hash == "sha256:" + "3" * 64


def test_effective_profile_projections_reads_compatibility_profile_view(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Compatibility views without a projections attribute remain readable."""
    import core_runtime.resolved_profile_scope as scope

    expected = ({"projection_id": "profile-content"},)
    monkeypatch.setattr(
        scope,
        "persisted_resolved_profile",
        lambda: SimpleNamespace(
            profile={"content_projections": expected},
        ),
    )

    assert effective_profile_projections() == expected


def test_persisted_profile_cache_tracks_activation_and_invalidation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import core_runtime.resolved_profile_scope as scope
    import core_runtime.bootstrap.profile_capture as capture_module

    settings_dir = tmp_path / "settings"
    settings_dir.mkdir()
    (settings_dir / "startup_profiles.json").write_text(
        json.dumps(
            {
                "active_profile_id": "fixture",
                "profiles": [
                    {
                        "profile_id": "fixture",
                        "base_pack": "defaultspack",
                        "packs": [],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(scope, "_PERSISTED_PROFILE_CACHE", None)
    monkeypatch.setattr(
        scope,
        "_PERSISTED_PROFILE_INVALIDATION_REVISION",
        0,
    )
    activation = {"value": _fake_v4_activation()}
    monkeypatch.setattr(
        capture_module,
        "capture_active_profile",
        lambda: activation["value"],
    )

    first = persisted_resolved_profile()
    cached = persisted_resolved_profile()
    assert first is cached

    activation["value"] = _fake_v4_activation("activation:defaults-next")
    authority_changed = persisted_resolved_profile()
    assert authority_changed is not None and authority_changed is not first

    invalidate_persisted_resolved_profile()
    invalidated = persisted_resolved_profile()
    assert invalidated is not authority_changed


def test_resolution_is_deterministic_immutable_and_dependency_complete(
    captured_v4_profile,
) -> None:
    """The captured v4 Profile, Lock, and Plan form one exact graph."""
    first = captured_v4_profile
    second = captured_v4_profile

    assert first.profile_id == second.profile_id == "defaults"
    assert first.plan_hash == second.plan_hash == first.plan["plan_digest"]
    assert first.profile["state"] == "resolved"
    assert tuple(sorted(item["identity"] for item in first.lock["effective_set"])) == tuple(
        sorted(first.effective_pack_set)
    )
    assert {item["pack_id"] for item in first.plan["bindings"]} <= set(
        first.effective_pack_set
    )
    with pytest.raises((AttributeError, TypeError)):
        first.profile_id = "changed"


def test_selection_is_not_an_authority_grant(captured_v4_profile) -> None:
    """A selected v4 composition is denied when Host authority is absent."""
    from ecosystem.defaultspack.domain.runtime_v4 import (
        ProfileResolutionDenied,
        resolve_default_profile,
    )

    snapshot = captured_v4_profile
    assert snapshot.profile["state"] == "resolved"
    catalog = load_packaged_profile_catalog()
    with pytest.raises(ProfileResolutionDenied, match="Authority Kernel reference"):
        resolve_default_profile(
            catalog,
            "defaults",
            approved_artifact_digests={
                str(manifest["pack"]["artifact_digest"])
                for manifest in catalog.packs.values()
            },
            authority_snapshot_digest=snapshot.profile[
                "profile_authority_snapshot_digest"
            ],
            authority_bindings={},
            security_epoch=1,
        )


def test_runtime_scope_is_bound_to_one_plan_revision(captured_v4_profile) -> None:
    """Runtime access follows the captured v4 plan, not filesystem selection."""
    from core_runtime.resolved_profile_scope import (
        active_resolved_profile,
        require_effective_pack,
    )

    snapshot = captured_v4_profile
    assert active_resolved_profile() is snapshot
    require_effective_pack(snapshot.effective_pack_set[0])
    with pytest.raises(PermissionError, match="outside resolved Profile"):
        require_effective_pack("unbound-test-pack")


def test_pack_removal_removes_every_projection(captured_v4_profile) -> None:
    """The active v4 graph exposes no legacy resource projection surface."""
    snapshot = captured_v4_profile
    assert tuple(sorted(item["identity"] for item in snapshot.lock["effective_set"])) == tuple(
        sorted(snapshot.effective_pack_set)
    )
    assert snapshot.plan["bindings"]
    with pytest.raises(AttributeError):
        snapshot.projections
    defaultspack_root = Path(__file__).resolve().parent.parent / "ecosystem" / "defaultspack"
    assert not (defaultspack_root / "ecosystem.json").exists()


def test_effective_permissions_are_policy_intersection(captured_v4_profile) -> None:
    """v4 binds exact operations; it does not project legacy permission tuples."""
    snapshot = captured_v4_profile
    assert "effective_permissions" not in snapshot.plan
    assert snapshot.plan["bindings"]
    assert len(snapshot.plan["bindings"]) == len(
        snapshot.profile["authority_references"]
    )
    assert all(
        isinstance(binding["operation_id"], str)
        and isinstance(binding["contract_id"], str)
        for binding in snapshot.plan["bindings"]
    )


def test_lockfile_detects_pack_content_and_profile_revision_changes(
    captured_v4_profile,
) -> None:
    """v4 lock and plan digests reject content or revision substitution."""
    from ecosystem.defaultspack.domain.runtime_v4 import (
        ActivationStore,
        ProfileResolutionDenied,
    )

    snapshot = captured_v4_profile
    ActivationStore._validate_record_graph(
        deepcopy(snapshot.profile),
        deepcopy(snapshot.lock),
        deepcopy(snapshot.plan),
    )

    changed_lock = deepcopy(snapshot.lock)
    changed_lock["effective_set"][0]["artifact_digest"] = "sha256:" + "0" * 64
    with pytest.raises(ProfileResolutionDenied, match="ProfileLock digest is stale"):
        ActivationStore._validate_record_graph(
            deepcopy(snapshot.profile), changed_lock, deepcopy(snapshot.plan)
        )

    changed_profile = deepcopy(snapshot.profile)
    changed_profile["display_name"] = "tampered"
    with pytest.raises(ProfileResolutionDenied, match="ProfileLock or ResolvedPlan is stale"):
        ActivationStore._validate_record_graph(
            changed_profile, deepcopy(snapshot.lock), deepcopy(snapshot.plan)
        )


def test_legacy_selection_migration_has_dry_run_backup_and_rollback(
    tmp_path: Path,
) -> None:
    import core_runtime.resolved_profile as runtime_resolver

    assert not hasattr(runtime_resolver, "apply_legacy_selection_migration")
    assert not hasattr(runtime_resolver, "plan_legacy_selection_migration")
    assert not hasattr(runtime_resolver, "rollback_legacy_selection_migration")
    profile_path = tmp_path / "profile.json"
    selection_path = tmp_path / "setup_pack_selection.json"
    original = {"profile_id": "fixture", "packs": ["pack-a"], "user_edit": 7}
    profile_path.write_text(json.dumps(original), encoding="utf-8")
    selection_path.write_text(
        json.dumps({"setup_pack_ids": ["pack-b"]}), encoding="utf-8"
    )

    dry_run = plan_legacy_selection_migration(
        original, {"setup_pack_ids": ["pack-b"]}
    )
    assert dry_run.after_pack_ids == ("pack-a", "pack-b")
    assert json.loads(profile_path.read_text(encoding="utf-8")) == original

    applied = apply_legacy_selection_migration(
        profile_path, selection_path, backup_dir=tmp_path / "backups"
    )
    migrated = json.loads(profile_path.read_text(encoding="utf-8"))
    assert migrated["user_edit"] == 7
    assert migrated["packs"] == ["pack-a", "pack-b"]
    assert applied.backup_path is not None

    rollback_legacy_selection_migration(
        profile_path, Path(applied.backup_path)
    )
    assert json.loads(profile_path.read_text(encoding="utf-8")) == original
