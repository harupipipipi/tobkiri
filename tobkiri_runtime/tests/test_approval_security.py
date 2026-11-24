"""
test_approval_security.py - S-9, M-12 のテスト

S-9: HMAC 検証失敗が audit log に記録される
M-12: scan_packs 中に別スレッドから get_status() が即座に返る
"""
from __future__ import annotations

import json
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from core_runtime.approval_manager import (
    ApprovalManager,
    PackApproval,
    PackStatus,
)
from core_runtime.hmac_key_manager import compute_data_hmac


# ------------------------------------------------------------------ #
# Fixtures
# ------------------------------------------------------------------ #

@pytest.fixture()
def tmp_dirs(tmp_path):
    packs_dir = tmp_path / "ecosystem"
    grants_dir = tmp_path / "grants"
    packs_dir.mkdir()
    grants_dir.mkdir()
    return packs_dir, grants_dir


@pytest.fixture()
def secret_key():
    return "test-secret-key-for-hmac-verification"


@pytest.fixture()
def manager(tmp_dirs, secret_key):
    packs_dir, grants_dir = tmp_dirs
    am = ApprovalManager(
        packs_dir=str(packs_dir),
        grants_dir=str(grants_dir),
        secret_key=secret_key,
    )
    return am


def test_verified_pack_trust_is_derived_from_host_verification(
    manager, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        manager,
        "_is_core_pack",
        lambda pack_id: pack_id == "core_control_panel",
    )
    monkeypatch.setattr(
        manager,
        "_is_system_pack",
        lambda pack_id: pack_id == "system_ui",
    )
    monkeypatch.setattr(
        manager,
        "is_pack_approved_and_verified",
        lambda pack_id: (pack_id == "approved_pack", None),
    )

    assert manager.get_verified_pack_trust(
        [
            "core_control_panel",
            "system_ui",
            "approved_pack",
            "unapproved_pack",
        ]
    ) == {
        "core_control_panel": "system",
        "system_ui": "system",
        "approved_pack": "verified",
    }


# ------------------------------------------------------------------ #
# S-9: HMAC 検証失敗が audit log に記録される
# ------------------------------------------------------------------ #

class TestHmacVerificationFailureLogged:
    """S-9: HMAC 検証失敗時に audit log が記録されることを検証"""

    def test_missing_signature_logged(self, manager, tmp_dirs):
        """署名なしの grants ファイルで log_security_event が呼ばれる"""
        _, grants_dir = tmp_dirs

        # 署名なしの grants ファイルを作成
        grant_data = {
            "pack_id": "test_pack",
            "status": "approved",
            "created_at": "2025-01-01T00:00:00Z",
            "file_hashes": {},
            "permissions_requested": [],
        }
        grant_file = grants_dir / "test_pack.grants.json"
        grant_file.write_text(json.dumps(grant_data), encoding="utf-8")

        mock_logger = MagicMock()
        with patch(
            "core_runtime.audit_logger.get_audit_logger",
            return_value=mock_logger,
        ):
            manager._load_grant_file(grant_file)

        # audit log が呼ばれたことを検証
        mock_logger.log_security_event.assert_called_once()
        call_kwargs = mock_logger.log_security_event.call_args[1]
        assert call_kwargs["event_type"] == "hmac_verification_failed"
        assert call_kwargs["severity"] == "critical"
        assert call_kwargs["details"]["reason"] == "missing_signature"
        assert call_kwargs["pack_id"] == "test_pack"

        # ステータスが MODIFIED になっている
        assert manager._approvals["test_pack"].status == PackStatus.MODIFIED

    def test_signature_mismatch_logged(self, manager, tmp_dirs, secret_key):
        """署名不一致の grants ファイルで log_security_event が呼ばれる"""
        _, grants_dir = tmp_dirs

        grant_data = {
            "pack_id": "tampered_pack",
            "status": "approved",
            "created_at": "2025-01-01T00:00:00Z",
            "file_hashes": {},
            "permissions_requested": [],
        }
        # 正しい署名を計算
        sig = compute_data_hmac(secret_key.encode("utf-8"), grant_data)
        # 改ざん: 署名計算後にデータを変更
        grant_data["status"] = "blocked"
        grant_data["_hmac_signature"] = sig

        grant_file = grants_dir / "tampered_pack.grants.json"
        grant_file.write_text(json.dumps(grant_data), encoding="utf-8")

        mock_logger = MagicMock()
        with patch(
            "core_runtime.audit_logger.get_audit_logger",
            return_value=mock_logger,
        ):
            manager._load_grant_file(grant_file)

        mock_logger.log_security_event.assert_called_once()
        call_kwargs = mock_logger.log_security_event.call_args[1]
        assert call_kwargs["event_type"] == "hmac_verification_failed"
        assert call_kwargs["severity"] == "critical"
        assert call_kwargs["details"]["reason"] == "signature_mismatch"

        assert manager._approvals["tampered_pack"].status == PackStatus.MODIFIED


# ------------------------------------------------------------------ #
# M-12: scan_packs 中にロックがブロックされないことを検証
# ------------------------------------------------------------------ #

class TestScanPacksIoOutsideLock:
    """M-12: scan_packs のファイル I/O 中にロックが保持されていない"""

    def test_get_status_not_blocked_during_scan(self, manager, tmp_dirs):
        """scan_packs の I/O 中に get_status() が即座に返る"""
        packs_dir, _ = tmp_dirs

        # テスト用 Pack ディレクトリを作成
        pack_dir = packs_dir / "io_test_pack"
        pack_dir.mkdir()
        eco_json = pack_dir / "ecosystem.json"
        eco_json.write_text(
            json.dumps({"pack_id": "io_test_pack"}), encoding="utf-8",
        )

        # 既存の approval を設定して get_status で取得できる状態にする
        manager._approvals["pre_existing"] = PackApproval(
            pack_id="pre_existing",
            status=PackStatus.APPROVED,
            created_at="2025-01-01T00:00:00Z",
        )

        original_save = manager._save_grant
        get_status_results = []
        get_status_times = []

        def slow_save(approval):
            """_save_grant を遅くして、ロック解放を確認"""
            time.sleep(0.15)
            original_save(approval)

        manager._save_grant = slow_save

        def check_get_status():
            """別スレッドから get_status() を呼ぶ"""
            start = time.monotonic()
            result = manager.get_status("pre_existing")
            elapsed = time.monotonic() - start
            get_status_results.append(result)
            get_status_times.append(elapsed)

        # scan_packs を別スレッドで開始
        scan_thread = threading.Thread(target=manager.scan_packs)
        scan_thread.start()

        # 少し待ってから get_status を呼ぶ（scan_packs の I/O 中のはず）
        time.sleep(0.05)
        check_thread = threading.Thread(target=check_get_status)
        check_thread.start()

        check_thread.join(timeout=3.0)
        scan_thread.join(timeout=5.0)

        # get_status が値を返せた
        assert len(get_status_results) == 1
        assert get_status_results[0] == PackStatus.APPROVED

        # get_status が高速に返った（ロックにブロックされていない）
        # slow_save は 0.15s かかるので、ブロックされていたら 0.15s 以上かかる
        assert get_status_times[0] < 0.1, (
            f"get_status took {get_status_times[0]:.3f}s — "
            f"likely blocked by lock during I/O"
        )
