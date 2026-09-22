from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _write_staging_meta(tmp_path, staging_id, detected_pack_ids, changed_paths=None):
    staging_dir = tmp_path / "staging" / staging_id
    staging_dir.mkdir(parents=True, exist_ok=True)
    meta = {
        "staging_id": staging_id,
        "detected_pack_ids": detected_pack_ids,
        "changed_paths": list(changed_paths or []),
        "is_multi_pack": False,
    }
    (staging_dir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return meta


def test_dynamic_api_route_loader_is_physically_absent():
    from core_runtime.pack_api_server import PackAPIHandler

    assert not hasattr(PackAPIHandler, "load_api_routes")
    assert not hasattr(PackAPIHandler, "_api_route_exact")


def test_dynamic_api_route_dispatch_is_physically_absent():
    from core_runtime.pack_api_server import PackAPIHandler

    assert not hasattr(PackAPIHandler, "_dispatch_api_route")
    assert not hasattr(PackAPIHandler, "_is_pack_approved_for_runtime_routes")


def test_dynamic_web_mount_state_is_physically_absent():
    from core_runtime.pack_api_server import PackAPIHandler

    assert not hasattr(PackAPIHandler, "_web_mounts")
    assert not hasattr(PackAPIHandler, "load_web_mounts")


def test_static_mounts_are_finite_first_party_roots():
    from core_runtime.pack_api_server import PackAPIHandler

    handler = object.__new__(PackAPIHandler)
    assert handler._match_web_mount("/stale/index.html") is None
    assert {
        mount["path_prefix"] for mount in handler._fixed_web_mounts()
    } == {"/panel", "/setup", "/desktops"}


def test_dynamic_pre_auth_table_is_physically_absent():
    from core_runtime.pack_api_server import PackAPIHandler

    assert not hasattr(PackAPIHandler, "_pre_auth_table")
    assert not hasattr(PackAPIHandler, "load_pre_auth_routes")


def test_legacy_pre_auth_matcher_is_physically_absent():
    from core_runtime.pack_api_server import PackAPIHandler

    assert not hasattr(PackAPIHandler, "_is_pre_auth_route")
    assert PackAPIHandler._retired_api_path("/api/packs/scan") is True


def test_function_registry_trusts_manifest_entrypoint_file(tmp_path):
    from tests.legacy_authority_contracts import (
        assert_profile_resolver_requires_authority_snapshot,
        assert_retired_module_absent,
    )
    from tests.v4_batch_support import assert_payload_mutations_denied, harness

    assert_retired_module_absent("core_runtime.function_registry")
    assert_profile_resolver_requires_authority_snapshot()
    assert_payload_mutations_denied(harness(tmp_path))


def test_function_registry_rejects_escaping_entrypoint(tmp_path):
    from tests.legacy_authority_contracts import (
        assert_profile_resolver_requires_authority_snapshot,
        assert_retired_module_absent,
    )
    from tests.v4_batch_support import assert_payload_mutations_denied, harness

    assert_retired_module_absent("core_runtime.function_registry")
    assert_profile_resolver_requires_authority_snapshot()
    assert_payload_mutations_denied(harness(tmp_path))


def test_staging_helpers_reject_path_like_ids(tmp_path):
    from core_runtime.pack_applier import PackApplier
    from core_runtime.pack_importer import PackImporter

    importer = PackImporter(staging_root=str(tmp_path / "staging"))
    applier = PackApplier(
        ecosystem_dir=str(tmp_path / "ecosystem"),
        backup_root=str(tmp_path / "backups"),
        staging_root=str(tmp_path / "staging"),
    )

    assert importer.get_staging_meta("../outside") is None
    assert importer.cleanup_staging("../outside") is False
    result = applier.apply("../outside")
    assert result.success is False
    assert "Invalid staging_id" in (result.error or "")


def test_pack_apply_revalidates_pack_id_from_staging_meta(tmp_path):
    from core_runtime.pack_applier import PackApplier

    staging_id = "a" * 16
    staging_root = tmp_path / "staging"
    staging_dir = staging_root / staging_id
    payload_dir = staging_dir / "payload"
    pack_dir = payload_dir / "safe_pack"
    pack_dir.mkdir(parents=True)
    (pack_dir / "ecosystem.json").write_text(
        json.dumps(
            {
                "pack_id": "safe_pack",
                "version": "1.0.0",
                "metadata": {"name": "Safe Pack"},
            }
        ),
        encoding="utf-8",
    )
    (staging_dir / "meta.json").write_text(
        json.dumps(
            {
                "staging_id": staging_id,
                "detected_pack_ids": ["../escape"],
                "is_multi_pack": False,
            }
        ),
        encoding="utf-8",
    )

    result = PackApplier(
        ecosystem_dir=str(tmp_path / "ecosystem"),
        backup_root=str(tmp_path / "backups"),
        staging_root=str(staging_root),
    ).apply(staging_id)

    assert result.success is False
    assert "Invalid pack_id" in (result.error or "")
    assert not (tmp_path / "escape").exists()


def test_pack_applier_audits_apply_actor(monkeypatch, tmp_path):
    from core_runtime.pack_applier import PackApplier

    staging_id = "a" * 16
    staging_root = tmp_path / "staging"
    staging_dir = staging_root / staging_id
    pack_dir = staging_dir / "payload" / "safe_pack"
    pack_dir.mkdir(parents=True)
    (pack_dir / "ecosystem.json").write_text(
        json.dumps(
            {
                "pack_id": "safe_pack",
                "version": "1.0.0",
                "metadata": {"name": "Safe Pack"},
            }
        ),
        encoding="utf-8",
    )
    (staging_dir / "meta.json").write_text(
        json.dumps(
            {
                "staging_id": staging_id,
                "detected_pack_ids": ["safe_pack"],
                "is_multi_pack": False,
            }
        ),
        encoding="utf-8",
    )
    audit_events = []

    class _Audit:
        def log_system_event(self, **kwargs):
            audit_events.append(kwargs)

    monkeypatch.setattr(
        "core_runtime.audit_logger.get_audit_logger",
        lambda: _Audit(),
    )
    monkeypatch.setattr(
        "core_runtime.approval_manager.get_approval_manager",
        lambda: SimpleNamespace(mark_modified=lambda _pack_id: None),
    )

    result = PackApplier(
        ecosystem_dir=str(tmp_path / "ecosystem"),
        backup_root=str(tmp_path / "backups"),
        staging_root=str(staging_root),
    ).apply(staging_id, actor="profile:work__surface:mobile")

    assert result.success is True
    assert [event["event_type"] for event in audit_events] == [
        "pack_apply_started",
        "pack_apply_completed",
    ]
    assert all(
        event["details"]["actor"] == "profile:work__surface:mobile"
        for event in audit_events
    )


def test_defaultspack_management_aliases_do_not_need_runtime_registry(monkeypatch):
    pack_root = Path(__file__).resolve().parent.parent / "ecosystem" / "defaultspack"
    monkeypatch.syspath_prepend(str(pack_root))

    from core_runtime.di_container import reset_container
    from domain.function_runtime.dispatcher import run_defaultspack_function

    reset_container()
    result = run_defaultspack_function(
        "pack_request_list",
        {},
        {"pack_id": "defaultspack"},
    )

    assert result["status"] == "ok"
    assert "requests" in result["data"]


def test_extension_manager_rejects_unsafe_request_ids(tmp_path):
    from ecosystem.defaultspack.backend.pack_extension.extension_manager import ExtensionManager

    manager = ExtensionManager(
        requests_root=tmp_path / "requests",
        ecosystem_dir=tmp_path / "ecosystem",
        backup_root=tmp_path / "backups",
        staging_root=tmp_path / "staging",
    )

    with pytest.raises(ValueError):
        manager._request_path("../outside")

    safe_path = manager._request_path("req_" + "a" * 16)
    safe_path.resolve().relative_to((tmp_path / "requests").resolve())
    assert manager.get_request("../outside")["status_code"] == 400
    assert manager.approve_request("../outside")["status_code"] == 400
    assert manager.rollback_request("../outside")["status_code"] == 400


def test_extension_manager_rejects_mismatched_request_file_id(tmp_path):
    from ecosystem.defaultspack.backend.pack_extension.extension_manager import ExtensionManager

    requests_root = tmp_path / "requests"
    requests_root.mkdir()
    (requests_root / ("req_" + "a" * 16 + ".json")).write_text(
        json.dumps(
            {
                "request_id": "../outside",
                "mode": "request_extension",
                "actor": "tester",
                "target_pack_id": "safe_pack",
                "notes": "bad",
            }
        ),
        encoding="utf-8",
    )
    manager = ExtensionManager(
        requests_root=requests_root,
        ecosystem_dir=tmp_path / "ecosystem",
        backup_root=tmp_path / "backups",
        staging_root=tmp_path / "staging",
    )

    result = manager.get_request("req_" + "a" * 16)

    assert result["status_code"] == 404


def test_rollback_revalidates_applied_pack_ids(tmp_path):
    from ecosystem.defaultspack.backend.pack_extension.extension_manager import (
        ExtensionManager,
        ExtensionRequest,
        PatchMode,
    )

    outside = tmp_path / "escape"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep", encoding="utf-8")
    manager = ExtensionManager(
        requests_root=tmp_path / "requests",
        ecosystem_dir=tmp_path / "ecosystem",
        backup_root=tmp_path / "backups",
        staging_root=tmp_path / "staging",
    )
    request = ExtensionRequest(
        request_id="req_" + "a" * 16,
        mode=PatchMode.REQUEST_EXTENSION,
        pack_id="tester",
        target_pack_id="safe_pack",
        summary="bad rollback",
        status="applied",
        applied_pack_ids=["../escape"],
    )
    manager._write_request(request)

    result = manager.rollback_request(request.request_id)

    assert result["status_code"] == 400
    assert "Invalid pack_id" in result["error"]
    assert (outside / "keep.txt").exists()


def test_create_pack_request_snapshots_staging_meta(tmp_path):
    from ecosystem.defaultspack.backend.pack_extension.extension_manager import (
        ExtensionManager,
        PatchMode,
    )

    staging_id = "a" * 16
    _write_staging_meta(tmp_path, staging_id, ["nice_pack"], ["ecosystem.json"])
    manager = ExtensionManager(
        requests_root=tmp_path / "requests",
        ecosystem_dir=tmp_path / "ecosystem",
        backup_root=tmp_path / "backups",
        staging_root=tmp_path / "staging",
    )

    created = manager.create_pack_request(
        mode=PatchMode.REQUEST_EXTENSION.value,
        staging_id=staging_id,
        actor="tester",
        target_pack_id="nice_pack",
    )

    assert created["request_id"] == "req_" + staging_id
    assert created["detected_pack_ids"] == ["nice_pack"]
    assert created["changed_paths"] == ["ecosystem.json"]
    assert len(created["staging_meta_sha256"]) == 64


def test_create_pack_request_rejects_target_pack_mismatch(tmp_path):
    from ecosystem.defaultspack.backend.pack_extension.extension_manager import (
        ExtensionManager,
        PatchMode,
    )

    staging_id = "a" * 16
    _write_staging_meta(tmp_path, staging_id, ["nice_pack"])
    manager = ExtensionManager(
        requests_root=tmp_path / "requests",
        ecosystem_dir=tmp_path / "ecosystem",
        backup_root=tmp_path / "backups",
        staging_root=tmp_path / "staging",
    )

    result = manager.create_pack_request(
        mode=PatchMode.REQUEST_EXTENSION.value,
        staging_id=staging_id,
        actor="tester",
        target_pack_id="evil_pack",
    )

    assert result["status_code"] == 400
    assert "target_pack_id" in result["error"]


def test_approve_request_rechecks_staging_meta_before_apply(monkeypatch, tmp_path):
    from ecosystem.defaultspack.backend.pack_extension.extension_manager import (
        ExtensionManager,
        PatchMode,
    )

    staging_id = "a" * 16
    _write_staging_meta(tmp_path, staging_id, ["nice_pack"])
    calls = []

    class _Applier:
        def __init__(self, **kwargs):
            calls.append(("init", kwargs))

        def apply(self, staging_id, *, mode="replace", actor="api_user"):
            calls.append(("apply", staging_id, mode, actor))
            raise AssertionError("apply should not run after staging metadata changes")

    monkeypatch.setattr("core_runtime.pack_applier.PackApplier", _Applier)
    manager = ExtensionManager(
        requests_root=tmp_path / "requests",
        ecosystem_dir=tmp_path / "ecosystem",
        backup_root=tmp_path / "backups",
        staging_root=tmp_path / "staging",
    )
    created = manager.create_pack_request(
        mode=PatchMode.REQUEST_EXTENSION.value,
        staging_id=staging_id,
        actor="tester",
        target_pack_id="nice_pack",
    )
    _write_staging_meta(tmp_path, staging_id, ["evil_pack"])

    result = manager.approve_request(created["request_id"], reviewer="reviewer")

    assert result["status_code"] == 409
    assert "staging metadata changed" in result["error"]
    assert calls == []


def test_extension_approval_applies_staging(monkeypatch, tmp_path):
    from ecosystem.defaultspack.backend.pack_extension.extension_manager import (
        ExtensionManager,
        PatchMode,
    )

    calls = []

    class _ApplyResult:
        success = True
        applied_pack_ids = ["new_pack"]
        backup_paths = {"old_pack": str(tmp_path / "backups" / "old_pack")}

        def to_dict(self):
            return {
                "success": True,
                "applied_pack_ids": self.applied_pack_ids,
                "backup_paths": self.backup_paths,
            }

    class _Applier:
        def __init__(self, *, ecosystem_dir, backup_root, staging_root):
            calls.append(("init", ecosystem_dir, backup_root, staging_root))

        def apply(self, staging_id, *, mode="replace", actor="api_user"):
            calls.append(("apply", staging_id, mode, actor))
            return _ApplyResult()

    monkeypatch.setattr("core_runtime.pack_applier.PackApplier", _Applier)
    _write_staging_meta(tmp_path, "a" * 16, ["new_pack"])
    manager = ExtensionManager(
        requests_root=tmp_path / "requests",
        ecosystem_dir=tmp_path / "ecosystem",
        backup_root=tmp_path / "backups",
        staging_root=tmp_path / "staging",
    )
    created = manager.create_pack_request(
        mode=PatchMode.REQUEST_EXTENSION.value,
        staging_id="a" * 16,
        actor="tester",
        target_pack_id="new_pack",
    )

    result = manager.approve_request(created["request_id"], reviewer="reviewer")

    assert result["status"] == "applied"
    assert result["applied_pack_ids"] == ["new_pack"]
    assert calls[0] == (
        "init",
        str(tmp_path / "ecosystem"),
        str(tmp_path / "backups"),
        str(tmp_path / "staging"),
    )
    assert calls[-1] == ("apply", "a" * 16, "replace", "reviewer")


def test_defaultspack_management_requires_captured_operation():
    from tests.v4_batch_support import assert_route_cutover

    assert_route_cutover(
        "GET",
        "/api/defaultspack/modules",
        "tobkiri.pack-management.v1",
        "defaultspack.pack-management.list-modules",
    )
