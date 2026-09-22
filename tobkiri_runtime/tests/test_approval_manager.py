"""
test_approval_manager.py - P0: ApprovalManager のテスト

対象: core_runtime/approval_manager.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core_runtime.approval_manager import (  # noqa: E402
    ApprovalManager,
    PackApproval,
    PackStatus,
)

TRUSTED_BUILTIN_PACK_IDS = ("defaultspack", "rumi_default_tools_pack")


def test_trusted_builtin_packs_are_approved_without_user_grants(tmp_path):
    ecosystem_dir = tmp_path / "bundle" / "app" / "ecosystem"
    for pack_id in TRUSTED_BUILTIN_PACK_IDS:
        _make_pack_dir(ecosystem_dir, pack_id)
    mgr = ApprovalManager(
        packs_dir=str(ecosystem_dir),
        grants_dir=str(tmp_path / "grants"),
        secret_key="test-secret-key-for-hmac",
    )

    for pack_id in TRUSTED_BUILTIN_PACK_IDS:
        assert mgr.get_status(pack_id) == PackStatus.APPROVED
        assert mgr.verify_hash(pack_id) is True
        assert mgr.verify_hash_detailed(pack_id)["valid"] is True
        assert pack_id in mgr.get_approved_pack_ids()


@pytest.mark.parametrize("pack_id", TRUSTED_BUILTIN_PACK_IDS)
def test_repo_ecosystem_trusted_builtin_pack_is_approved_without_user_grants(tmp_path, pack_id):
    ecosystem_dir = PROJECT_ROOT / "ecosystem"
    assert (ecosystem_dir / pack_id).is_dir()

    mgr = ApprovalManager(
        packs_dir=str(ecosystem_dir),
        grants_dir=str(tmp_path / "grants"),
        secret_key="test-secret-key-for-hmac",
    )

    assert mgr.get_approval(pack_id) is None
    assert mgr.get_status(pack_id) == PackStatus.APPROVED
    assert mgr.verify_hash(pack_id) is True
    assert mgr.verify_hash_detailed(pack_id)["valid"] is True
    assert pack_id in mgr.get_approved_pack_ids()
    assert not (tmp_path / "grants" / f"{pack_id}.grants.json").exists()


# ===================================================================
# Helper
# ===================================================================

def _make_pack_dir(base: Path, pack_id: str = "testpack") -> Path:
    """テスト用の Pack ディレクトリを作成する"""
    pack_dir = base / pack_id
    pack_dir.mkdir(parents=True, exist_ok=True)
    (pack_dir / "ecosystem.json").write_text(
        json.dumps({"pack_id": pack_id, "version": "1.0"}),
        encoding="utf-8",
    )
    (pack_dir / "handler.py").write_text(
        "def run(): pass\n", encoding="utf-8"
    )
    return pack_dir


def _make_manager(
    tmp_path: Path,
    pack_id: str = "testpack",
    monkeypatch=None,
) -> tuple:
    """ApprovalManager + Pack ディレクトリをセットアップする"""
    eco_dir = tmp_path / "eco"
    grants_dir = tmp_path / "grants"
    eco_dir.mkdir(parents=True, exist_ok=True)
    grants_dir.mkdir(parents=True, exist_ok=True)

    pack_dir = _make_pack_dir(eco_dir, pack_id)

    mgr = ApprovalManager(
        packs_dir=str(eco_dir),
        grants_dir=str(grants_dir),
        secret_key="test-secret-key-for-hmac",
    )

    # _create_declared_stores は store_registry に遅延依存するため無効化
    if monkeypatch is not None:
        monkeypatch.setattr(mgr, "_create_declared_stores", lambda pid: None)

    # Pack を approvals に手動登録（scan_packs の代替）
    mgr._approvals[pack_id] = PackApproval(
        pack_id=pack_id,
        status=PackStatus.INSTALLED,
        created_at="2026-01-01T00:00:00Z",
    )

    return mgr, pack_dir


# ===================================================================
# approve
# ===================================================================

class TestApprove:

    def test_approve_installed_pack(self, tmp_path, monkeypatch):
        mgr, pack_dir = _make_manager(tmp_path, monkeypatch=monkeypatch)
        result = mgr.approve("testpack")
        assert result.success is True
        assert result.status == PackStatus.APPROVED
        assert mgr.get_status("testpack") == PackStatus.APPROVED

    def test_approve_computes_hashes(self, tmp_path, monkeypatch):
        mgr, pack_dir = _make_manager(tmp_path, monkeypatch=monkeypatch)
        mgr.approve("testpack")
        approval = mgr.get_approval("testpack")
        assert approval is not None
        assert len(approval.file_hashes) > 0
        # handler.py と ecosystem.json がハッシュされているはず
        keys = list(approval.file_hashes.keys())
        assert any("handler.py" in k for k in keys)

    def test_approve_sets_approved_at(self, tmp_path, monkeypatch):
        mgr, _ = _make_manager(tmp_path, monkeypatch=monkeypatch)
        mgr.approve("testpack")
        approval = mgr.get_approval("testpack")
        assert approval.approved_at is not None

    def test_approve_nonexistent_pack(self, tmp_path, monkeypatch):
        mgr, _ = _make_manager(tmp_path, monkeypatch=monkeypatch)
        result = mgr.approve("nonexistent")
        assert result.success is False

    def test_approve_saves_grant_file(self, tmp_path, monkeypatch):
        mgr, _ = _make_manager(tmp_path, monkeypatch=monkeypatch)
        mgr.approve("testpack")
        grant_file = tmp_path / "grants" / "testpack.grants.json"
        assert grant_file.exists()
        data = json.loads(grant_file.read_text(encoding="utf-8"))
        assert data["status"] == "approved"
        assert "_hmac_signature" in data

    def test_approve_if_snapshot_accepts_exact_contents(self, tmp_path, monkeypatch):
        mgr, _ = _make_manager(tmp_path, monkeypatch=monkeypatch)
        snapshot = mgr.get_pack_approval_snapshot("testpack")

        result = mgr.approve_if_snapshot("testpack", snapshot["snapshot_digest"])

        assert result.success is True
        assert mgr.is_pack_approved_and_verified("testpack")[0] is True

    def test_approve_if_snapshot_rejects_changed_contents(self, tmp_path, monkeypatch):
        mgr, pack_dir = _make_manager(tmp_path, monkeypatch=monkeypatch)
        snapshot = mgr.get_pack_approval_snapshot("testpack")
        (pack_dir / "handler.py").write_text("def run(): return 'changed'\n", encoding="utf-8")

        result = mgr.approve_if_snapshot("testpack", snapshot["snapshot_digest"])

        assert result.success is False
        assert "changed" in str(result.error).lower()
        assert mgr.get_status("testpack") != PackStatus.APPROVED

    def test_approve_if_snapshot_rejects_external_write_between_compare_and_persist(
        self, tmp_path, monkeypatch
    ):
        mgr, pack_dir = _make_manager(tmp_path, monkeypatch=monkeypatch)
        snapshot = mgr.get_pack_approval_snapshot("testpack")
        original_persist = mgr._persist_approved_snapshot

        def replace_before_persist(pack_id, file_hashes):
            (pack_dir / "handler.py").write_text(
                "def run(): return 'raced'\n", encoding="utf-8"
            )
            return original_persist(pack_id, file_hashes)

        monkeypatch.setattr(mgr, "_persist_approved_snapshot", replace_before_persist)

        result = mgr.approve_if_snapshot(
            "testpack", snapshot["snapshot_digest"]
        )

        assert result.success is False
        assert result.status == PackStatus.MODIFIED
        approval = mgr.get_approval("testpack")
        assert approval is not None
        assert approval.file_hashes != mgr._compute_pack_hashes_nocache(pack_dir)


# ===================================================================
# reject
# ===================================================================

class TestReject:

    def test_reject_pack(self, tmp_path, monkeypatch):
        mgr, _ = _make_manager(tmp_path, monkeypatch=monkeypatch)
        result = mgr.reject("testpack", reason="unsafe")
        assert result.success is True
        assert result.status == PackStatus.BLOCKED
        assert mgr.get_status("testpack") == PackStatus.BLOCKED

    def test_reject_nonexistent_pack(self, tmp_path, monkeypatch):
        mgr, _ = _make_manager(tmp_path, monkeypatch=monkeypatch)
        result = mgr.reject("nonexistent")
        assert result.success is False

    def test_reject_saves_reason(self, tmp_path, monkeypatch):
        mgr, _ = _make_manager(tmp_path, monkeypatch=monkeypatch)
        mgr.reject("testpack", reason="malicious code")
        approval = mgr.get_approval("testpack")
        assert approval.rejection_reason == "malicious code"


# ===================================================================
# verify_hash
# ===================================================================

class TestVerifyHash:

    def test_hash_matches_after_approve(self, tmp_path, monkeypatch):
        mgr, _ = _make_manager(tmp_path, monkeypatch=monkeypatch)
        mgr.approve("testpack")
        assert mgr.verify_hash("testpack") is True

    def test_hash_mismatch_after_file_change(self, tmp_path, monkeypatch):
        mgr, pack_dir = _make_manager(tmp_path, monkeypatch=monkeypatch)
        mgr.approve("testpack")
        # Modify a file
        (pack_dir / "handler.py").write_text(
            "def run(): return 'evil'\n", encoding="utf-8"
        )
        # Invalidate cache so fresh hashes are computed
        mgr._hash_cache.clear()
        assert mgr.verify_hash("testpack") is False

    def test_hash_mismatch_after_file_added(self, tmp_path, monkeypatch):
        mgr, pack_dir = _make_manager(tmp_path, monkeypatch=monkeypatch)
        mgr.approve("testpack")
        (pack_dir / "extra.py").write_text("# new file", encoding="utf-8")
        mgr._hash_cache.clear()
        assert mgr.verify_hash("testpack") is False

    def test_runtime_user_data_inside_pack_does_not_change_hash(self, tmp_path, monkeypatch):
        mgr, pack_dir = _make_manager(tmp_path, monkeypatch=monkeypatch)
        mgr.approve("testpack")
        audit_dir = pack_dir / "functions" / "sample" / "user_data" / "audit"
        audit_dir.mkdir(parents=True)
        (audit_dir / "system_2026-05-11.jsonl").write_text(
            '{"event":"runtime audit"}\n',
            encoding="utf-8",
        )
        mgr._hash_cache.clear()
        assert mgr.verify_hash("testpack") is True

    def test_hash_mismatch_after_file_removed(self, tmp_path, monkeypatch):
        mgr, pack_dir = _make_manager(tmp_path, monkeypatch=monkeypatch)
        mgr.approve("testpack")
        (pack_dir / "handler.py").unlink()
        mgr._hash_cache.clear()
        assert mgr.verify_hash("testpack") is False

    def test_verify_hash_unapproved(self, tmp_path, monkeypatch):
        mgr, _ = _make_manager(tmp_path, monkeypatch=monkeypatch)
        # Not approved yet, no file_hashes stored
        assert mgr.verify_hash("testpack") is False

    def test_root_component_change_is_critical(self, tmp_path, monkeypatch):
        mgr, pack_dir = _make_manager(tmp_path, monkeypatch=monkeypatch)
        component_dir = pack_dir / "components" / "demo"
        component_dir.mkdir(parents=True)
        (component_dir / "startup.py").write_text(
            "def run(): return 'approved'\n", encoding="utf-8"
        )

        mgr.approve("testpack")
        approval = mgr.get_approval("testpack")
        assert approval is not None
        approval.file_hashes = {
            (path.replace("/", "\\") if path == "components/demo/startup.py" else path): value
            for path, value in approval.file_hashes.items()
        }
        (component_dir / "startup.py").write_text(
            "def run(): return 'tampered'\n", encoding="utf-8"
        )
        mgr._hash_cache.clear()

        result = mgr.verify_hash_detailed("testpack")
        assert result["valid"] is False
        assert result["critical_changed"] is True
        assert result["changed_files"] == ["components/demo/startup.py"]



# ===================================================================
# HMAC signature on grant files
# ===================================================================

class TestGrantHMAC:

    def test_grant_file_has_valid_hmac(self, tmp_path, monkeypatch):
        mgr, _ = _make_manager(tmp_path, monkeypatch=monkeypatch)
        mgr.approve("testpack")
        grant_file = tmp_path / "grants" / "testpack.grants.json"
        data = json.loads(grant_file.read_text(encoding="utf-8"))
        sig = data.pop("_hmac_signature")
        from core_runtime.hmac_key_manager import verify_data_hmac
        assert verify_data_hmac(b"test-secret-key-for-hmac", data, sig) is True

    def test_tampered_grant_detected_on_load(self, tmp_path, monkeypatch):
        mgr, _ = _make_manager(tmp_path, monkeypatch=monkeypatch)
        mgr.approve("testpack")

        # Tamper with the grant file
        grant_file = tmp_path / "grants" / "testpack.grants.json"
        data = json.loads(grant_file.read_text(encoding="utf-8"))
        data["status"] = "approved"
        data["_hmac_signature"] = "0" * 64  # invalid signature
        grant_file.write_text(json.dumps(data), encoding="utf-8")

        # Create new manager and load grants
        mgr2 = ApprovalManager(
            packs_dir=str(tmp_path / "eco"),
            grants_dir=str(tmp_path / "grants"),
            secret_key="test-secret-key-for-hmac",
        )
        monkeypatch.setattr(mgr2, "_create_declared_stores", lambda pid: None)
        mgr2.initialize()
        assert mgr2.get_status("testpack") == PackStatus.MODIFIED

    def test_unsigned_grant_detected_on_load(self, tmp_path, monkeypatch):
        mgr, _ = _make_manager(tmp_path, monkeypatch=monkeypatch)
        mgr.approve("testpack")

        # Remove HMAC from grant file
        grant_file = tmp_path / "grants" / "testpack.grants.json"
        data = json.loads(grant_file.read_text(encoding="utf-8"))
        data.pop("_hmac_signature", None)
        grant_file.write_text(json.dumps(data), encoding="utf-8")

        mgr2 = ApprovalManager(
            packs_dir=str(tmp_path / "eco"),
            grants_dir=str(tmp_path / "grants"),
            secret_key="test-secret-key-for-hmac",
        )
        monkeypatch.setattr(mgr2, "_create_declared_stores", lambda pid: None)
        mgr2.initialize()
        assert mgr2.get_status("testpack") == PackStatus.MODIFIED


# ===================================================================
# mark_modified / remove_approval / get_pending_packs
# ===================================================================

class TestMiscOperations:

    def test_mark_modified(self, tmp_path, monkeypatch):
        mgr, _ = _make_manager(tmp_path, monkeypatch=monkeypatch)
        mgr.approve("testpack")
        mgr.mark_modified("testpack")
        assert mgr.get_status("testpack") == PackStatus.MODIFIED

    def test_remove_approval(self, tmp_path, monkeypatch):
        mgr, _ = _make_manager(tmp_path, monkeypatch=monkeypatch)
        mgr.approve("testpack")
        assert mgr.remove_approval("testpack") is True
        assert mgr.get_status("testpack") is None
        grant_file = tmp_path / "grants" / "testpack.grants.json"
        assert not grant_file.exists()

    def test_remove_nonexistent(self, tmp_path, monkeypatch):
        mgr, _ = _make_manager(tmp_path, monkeypatch=monkeypatch)
        assert mgr.remove_approval("nonexistent") is False

    def test_remove_approval_rejects_path_traversal(self, tmp_path, monkeypatch):
        mgr, _ = _make_manager(tmp_path, monkeypatch=monkeypatch)
        pack_id = "../escape"
        mgr._approvals[pack_id] = PackApproval(
            pack_id=pack_id,
            status=PackStatus.APPROVED,
            created_at="2025-01-01T00:00:00Z",
        )

        assert mgr.remove_approval(pack_id) is False
        assert pack_id not in mgr._approvals

    def test_get_pending_packs(self, tmp_path, monkeypatch):
        mgr, _ = _make_manager(tmp_path, monkeypatch=monkeypatch)
        # INSTALLED counts as pending
        pending = mgr.get_pending_packs()
        assert "testpack" in pending

    def test_get_pending_excludes_approved(self, tmp_path, monkeypatch):
        mgr, _ = _make_manager(tmp_path, monkeypatch=monkeypatch)
        mgr.approve("testpack")
        pending = mgr.get_pending_packs()
        assert "testpack" not in pending

    def test_is_pack_approved_and_verified(self, tmp_path, monkeypatch):
        mgr, _ = _make_manager(tmp_path, monkeypatch=monkeypatch)
        mgr.approve("testpack")
        is_valid, reason = mgr.is_pack_approved_and_verified("testpack")
        assert is_valid is True
        assert reason is None

    def test_is_pack_not_approved(self, tmp_path, monkeypatch):
        mgr, _ = _make_manager(tmp_path, monkeypatch=monkeypatch)
        is_valid, reason = mgr.is_pack_approved_and_verified("testpack")
        assert is_valid is False
        assert reason == "not_approved"

    @pytest.mark.parametrize("pack_id", TRUSTED_BUILTIN_PACK_IDS)
    def test_non_bundled_builtin_named_pack_copy_requires_user_grant(self, tmp_path, monkeypatch, pack_id):
        mgr, _ = _make_manager(tmp_path, pack_id=pack_id, monkeypatch=monkeypatch)

        assert mgr.get_status(pack_id) == PackStatus.INSTALLED

        is_valid, reason = mgr.is_pack_approved_and_verified(pack_id)
        assert is_valid is False
        assert reason == "not_approved"
        assert mgr.verify_hash(pack_id) is False
        assert pack_id not in mgr.get_approved_pack_ids()

    def test_is_pack_blocked(self, tmp_path, monkeypatch):
        mgr, _ = _make_manager(tmp_path, monkeypatch=monkeypatch)
        mgr.reject("testpack")
        is_valid, reason = mgr.is_pack_approved_and_verified("testpack")
        assert is_valid is False
        assert reason == "blocked"
