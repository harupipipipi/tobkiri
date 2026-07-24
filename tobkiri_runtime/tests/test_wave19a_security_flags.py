"""
tests/test_wave19a_security_flags.py

W19-A: セキュリティフラグ修正のテスト
- VULN-C05: PermissionManager のデフォルト連動 (8件以上)
- VULN-C01: production 環境での permissive 拒否 (4件以上)
- VULN-H05: audit_logger の ensure_ascii=True (3件以上)
- host_execution 未承認 Pack の起動時拒否 (5件以上)

合計 20件以上
"""

import json
import logging
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# テスト対象のインポート
# ---------------------------------------------------------------------------
# PermissionManager は stdlib のみ依存なので直接インポートできる
from core_runtime.permission_manager import PermissionManager

# AuditEntry も直接インポート可能
from core_runtime.audit_logger import AuditEntry

# app.py のガード関数
from app import _check_permissive_production_guard

# pack_validator の validate_host_execution
from core_runtime.pack_validator import validate_host_execution


# ======================================================================
# VULN-C05: PermissionManager のデフォルト連動テスト (8件)
# ======================================================================

class TestVulnC05PermissionManagerDefaults:
    """VULN-C05: RUMI_SECURITY_MODE=strict 時のデフォルト連動"""

    def test_strict_mode_no_permission_mode_defaults_to_secure(self):
        """SECURITY_MODE=strict + PERMISSION_MODE 未設定 → secure"""
        env = {
            "RUMI_SECURITY_MODE": "strict",
        }
        # RUMI_PERMISSION_MODE を削除した状態
        clean_env = {k: v for k, v in os.environ.items()
                     if k not in ("RUMI_PERMISSION_MODE",)}
        clean_env.update(env)
        with patch.dict(os.environ, clean_env, clear=True):
            pm = PermissionManager()
            assert pm.get_mode() == "secure"

    def test_strict_mode_with_secure_permission_mode(self):
        """SECURITY_MODE=strict + PERMISSION_MODE=secure → secure"""
        env = {
            "RUMI_SECURITY_MODE": "strict",
            "RUMI_PERMISSION_MODE": "secure",
        }
        with patch.dict(os.environ, env, clear=True):
            pm = PermissionManager()
            assert pm.get_mode() == "secure"

    def test_strict_mode_with_explicit_permissive_warns(self):
        """SECURITY_MODE=strict + PERMISSION_MODE=permissive → permissive + WARNING"""
        env = {
            "RUMI_SECURITY_MODE": "strict",
            "RUMI_PERMISSION_MODE": "permissive",
        }
        with patch.dict(os.environ, env, clear=True):
            with patch("core_runtime.permission_manager.logger") as mock_logger:
                pm = PermissionManager()
                assert pm.get_mode() == "permissive"
                # VULN-C05 の特定 WARNING が出力されるか確認
                warning_calls = [
                    call for call in mock_logger.warning.call_args_list
                    if "RUMI_PERMISSION_MODE=permissive is explicitly set" in str(call)
                ]
                assert len(warning_calls) >= 1, (
                    "Expected VULN-C05 warning for strict+permissive combination"
                )

    def test_permissive_security_mode_defaults_to_permissive(self):
        """SECURITY_MODE=permissive + PERMISSION_MODE 未設定 → permissive"""
        env = {
            "RUMI_SECURITY_MODE": "permissive",
        }
        clean_env = {k: v for k, v in os.environ.items()
                     if k not in ("RUMI_PERMISSION_MODE",)}
        clean_env.update(env)
        with patch.dict(os.environ, clean_env, clear=True):
            pm = PermissionManager()
            assert pm.get_mode() == "permissive"

    def test_no_security_mode_defaults_strict_then_secure(self):
        """SECURITY_MODE 未設定 → デフォルト strict → secure 連動"""
        clean_env = {k: v for k, v in os.environ.items()
                     if k not in ("RUMI_SECURITY_MODE", "RUMI_PERMISSION_MODE")}
        with patch.dict(os.environ, clean_env, clear=True):
            pm = PermissionManager()
            assert pm.get_mode() == "secure"

    def test_secure_mode_has_permission_returns_false(self):
        """secure モードで has_permission() は未付与なら False"""
        env = {"RUMI_SECURITY_MODE": "strict"}
        clean_env = {k: v for k, v in os.environ.items()
                     if k not in ("RUMI_PERMISSION_MODE",)}
        clean_env.update(env)
        with patch.dict(os.environ, clean_env, clear=True):
            pm = PermissionManager()
            assert pm.get_mode() == "secure"
            assert pm.has_permission("test:tool:foo", "file_read") is False

    def test_permissive_mode_has_permission_returns_true(self):
        """permissive モードで has_permission() は常に True"""
        pm = PermissionManager(mode="permissive")
        assert pm.has_permission("test:tool:foo", "file_read") is True

    def test_mode_switch_after_init(self):
        """モード切替後の挙動が正しい"""
        pm = PermissionManager(mode="secure")
        assert pm.get_mode() == "secure"
        assert pm.has_permission("test:tool:foo", "file_read") is False

        pm.set_mode("permissive")
        assert pm.get_mode() == "permissive"
        assert pm.has_permission("test:tool:foo", "file_read") is True

        pm.set_mode("secure")
        assert pm.get_mode() == "secure"
        assert pm.has_permission("test:tool:foo", "file_read") is False


# ======================================================================
# VULN-C01: production 環境での permissive 拒否テスト (4件)
# ======================================================================

class TestVulnC01ProductionPermissiveGuard:
    """VULN-C01: production + --permissive → 起動拒否"""

    def test_production_permissive_exits(self):
        """RUMI_ENVIRONMENT=production → SystemExit"""
        with patch.dict(os.environ, {"RUMI_ENVIRONMENT": "production"}, clear=False):
            with pytest.raises(SystemExit) as exc_info:
                _check_permissive_production_guard()
            assert exc_info.value.code == 1

    def test_development_permissive_allowed(self, tmp_path):
        """RUMI_ENVIRONMENT=development + lockfile → 正常（exit しない）"""
        (tmp_path / "permissive.lock").touch()
        with patch.dict(os.environ, {"RUMI_ENVIRONMENT": "development", "RUMI_USER_DATA": str(tmp_path)}, clear=False):
            # 例外が発生しないことを確認
            _check_permissive_production_guard()

    def test_no_environment_permissive_allowed(self):
        """RUMI_ENVIRONMENT 未設定 → opt-in 不足で SystemExit"""
        clean_env = {k: v for k, v in os.environ.items()
                     if k not in ("RUMI_ENVIRONMENT", "RUMI_ALLOW_PERMISSIVE", "RUMI_USER_DATA")}
        with patch.dict(os.environ, clean_env, clear=True):
            with pytest.raises(SystemExit) as exc_info:
                _check_permissive_production_guard()
            assert exc_info.value.code == 1

    def test_staging_environment_permissive_allowed(self):
        """RUMI_ENVIRONMENT=staging → opt-in 対象外なので SystemExit"""
        with patch.dict(os.environ, {"RUMI_ENVIRONMENT": "staging"}, clear=False):
            with pytest.raises(SystemExit) as exc_info:
                _check_permissive_production_guard()
            assert exc_info.value.code == 1


# ======================================================================
# VULN-H05: audit_logger の ensure_ascii=True テスト (3件)
# ======================================================================

class TestVulnH05EnsureAscii:
    """VULN-H05: AuditEntry.to_json() で非ASCII文字がエスケープされる"""

    def test_non_ascii_escaped(self):
        """to_json() の出力に非ASCII文字が含まれない"""
        entry = AuditEntry(
            ts="2025-01-01T00:00:00Z",
            category="security",
            severity="info",
            action="test_action",
            success=True,
            owner_pack="テスト_パック",
            details={"description": "日本語テスト"},
        )
        json_str = entry.to_json()
        # 非ASCII文字がエスケープされていることを確認
        for ch in json_str:
            assert ord(ch) < 128, (
                f"Non-ASCII character found: U+{ord(ch):04X} '{ch}'"
            )
        # パースして元の値が復元されることを確認
        parsed = json.loads(json_str)
        assert parsed["owner_pack"] == "テスト_パック"
        assert parsed["details"]["description"] == "日本語テスト"

    def test_newline_in_pack_id_escaped(self):
        """改行を含む pack_id がエスケープされログインジェクション防止"""
        entry = AuditEntry(
            ts="2025-01-01T00:00:00Z",
            category="security",
            severity="warning",
            action="test_injection",
            success=False,
            owner_pack='evil_pack\n{"injected": true}',
        )
        json_str = entry.to_json()
        # 生の改行文字が含まれないこと
        assert "\n" not in json_str
        # パースして1つの有効なJSONオブジェクトであること
        parsed = json.loads(json_str)
        assert parsed["owner_pack"] == 'evil_pack\n{"injected": true}'

    def test_unicode_control_chars_escaped(self):
        """Unicode制御文字（U+2028 LINE SEPARATOR 等）がエスケープされる"""
        entry = AuditEntry(
            ts="2025-01-01T00:00:00Z",
            category="security",
            severity="info",
            action="test_unicode",
            success=True,
            details={
                "text_with_ls": "before\u2028after",
                "text_with_ps": "before\u2029after",
                "text_with_bom": "\uFEFFstart",
            },
        )
        json_str = entry.to_json()
        # 全て ASCII 範囲内
        for ch in json_str:
            assert ord(ch) < 128, (
                f"Non-ASCII character found: U+{ord(ch):04X} '{ch}'"
            )
        # パースして元の値が復元される
        parsed = json.loads(json_str)
        assert parsed["details"]["text_with_ls"] == "before\u2028after"
        assert parsed["details"]["text_with_ps"] == "before\u2029after"


# ======================================================================
# host_execution 未承認 Pack の起動時拒否テスト (5件)
# ======================================================================

def _create_ecosystem(tmp_dir: Path, packs: dict) -> str:
    """
    テスト用のエコシステムディレクトリを作成する。

    Args:
        tmp_dir: 一時ディレクトリ
        packs: {pack_id: ecosystem_json_dict} の辞書

    Returns:
        エコシステムディレクトリのパス文字列
    """
    eco_dir = tmp_dir / "ecosystem"
    eco_dir.mkdir(parents=True, exist_ok=True)
    for pack_id, eco_data in packs.items():
        pack_dir = eco_dir / pack_id
        pack_dir.mkdir(parents=True, exist_ok=True)
        eco_file = pack_dir / "ecosystem.json"
        eco_file.write_text(json.dumps(eco_data), encoding="utf-8")
    return str(eco_dir)


class TestHostExecutionGuard:
    """host_execution: true Pack の起動時拒否ガード"""

    def test_host_execution_true_no_env_exits(self):
        """host_execution: true + RUMI_ALLOW_HOST_EXECUTION 未設定 → 拒否"""
        with tempfile.TemporaryDirectory() as tmp:
            eco_dir = _create_ecosystem(Path(tmp), {
                "dangerous_pack": {
                    "pack_id": "dangerous_pack",
                    "host_execution": True,
                },
            })
            clean_env = {k: v for k, v in os.environ.items()
                         if k != "RUMI_ALLOW_HOST_EXECUTION"}
            with patch.dict(os.environ, clean_env, clear=True):
                with pytest.raises(SystemExit) as exc_info:
                    validate_host_execution(ecosystem_dir=eco_dir)
                assert exc_info.value.code == 1

    def test_host_execution_true_with_env_allowed(self):
        """host_execution: true + RUMI_ALLOW_HOST_EXECUTION=true → 許可 + WARNING"""
        with tempfile.TemporaryDirectory() as tmp:
            eco_dir = _create_ecosystem(Path(tmp), {
                "dangerous_pack": {
                    "pack_id": "dangerous_pack",
                    "host_execution": True,
                },
            })
            with patch.dict(os.environ, {"RUMI_ALLOW_HOST_EXECUTION": "true"}, clear=False):
                result = validate_host_execution(ecosystem_dir=eco_dir)
                assert "dangerous_pack" in result

    def test_host_execution_false_normal(self):
        """host_execution: false → 通常動作（空リスト）"""
        with tempfile.TemporaryDirectory() as tmp:
            eco_dir = _create_ecosystem(Path(tmp), {
                "safe_pack": {
                    "pack_id": "safe_pack",
                    "host_execution": False,
                },
            })
            clean_env = {k: v for k, v in os.environ.items()
                         if k != "RUMI_ALLOW_HOST_EXECUTION"}
            with patch.dict(os.environ, clean_env, clear=True):
                result = validate_host_execution(ecosystem_dir=eco_dir)
                assert result == []

    def test_host_execution_field_missing_normal(self):
        """host_execution フィールドなし → 通常動作（空リスト）"""
        with tempfile.TemporaryDirectory() as tmp:
            eco_dir = _create_ecosystem(Path(tmp), {
                "normal_pack": {
                    "pack_id": "normal_pack",
                },
            })
            clean_env = {k: v for k, v in os.environ.items()
                         if k != "RUMI_ALLOW_HOST_EXECUTION"}
            with patch.dict(os.environ, clean_env, clear=True):
                result = validate_host_execution(ecosystem_dir=eco_dir)
                assert result == []

    def test_mixed_host_execution_packs(self):
        """複数 Pack で host_execution 混在時: true のもののみ検出"""
        with tempfile.TemporaryDirectory() as tmp:
            eco_dir = _create_ecosystem(Path(tmp), {
                "safe_pack_a": {
                    "pack_id": "safe_pack_a",
                    "host_execution": False,
                },
                "dangerous_pack_b": {
                    "pack_id": "dangerous_pack_b",
                    "host_execution": True,
                },
                "normal_pack_c": {
                    "pack_id": "normal_pack_c",
                },
                "dangerous_pack_d": {
                    "pack_id": "dangerous_pack_d",
                    "host_execution": True,
                },
            })
            with patch.dict(os.environ, {"RUMI_ALLOW_HOST_EXECUTION": "true"}, clear=False):
                result = validate_host_execution(ecosystem_dir=eco_dir)
                assert sorted(result) == ["dangerous_pack_b", "dangerous_pack_d"]

    def test_only_selected_profile_packs_are_checked(self):
        """Unselected host-execution Packs must not block an unrelated profile."""
        with tempfile.TemporaryDirectory() as tmp:
            eco_dir = _create_ecosystem(Path(tmp), {
                "selected_safe_pack": {
                    "pack_id": "selected_safe_pack",
                    "host_execution": False,
                },
                "unselected_host_pack": {
                    "pack_id": "unselected_host_pack",
                    "host_execution": True,
                },
            })
            clean_env = {
                key: value for key, value in os.environ.items()
                if key != "RUMI_ALLOW_HOST_EXECUTION"
            }
            with patch.dict(os.environ, clean_env, clear=True):
                result = validate_host_execution(
                    ecosystem_dir=eco_dir,
                    pack_ids={"selected_safe_pack"},
                )
                assert result == []

    def test_host_execution_env_case_insensitive(self):
        """RUMI_ALLOW_HOST_EXECUTION=TRUE (大文字) も許可"""
        with tempfile.TemporaryDirectory() as tmp:
            eco_dir = _create_ecosystem(Path(tmp), {
                "host_pack": {
                    "pack_id": "host_pack",
                    "host_execution": True,
                },
            })
            with patch.dict(os.environ, {"RUMI_ALLOW_HOST_EXECUTION": "TRUE"}, clear=False):
                result = validate_host_execution(ecosystem_dir=eco_dir)
                assert "host_pack" in result

    def test_empty_ecosystem_no_error(self):
        """Pack が存在しないエコシステムでもエラーにならない"""
        with tempfile.TemporaryDirectory() as tmp:
            eco_dir = _create_ecosystem(Path(tmp), {})
            result = validate_host_execution(ecosystem_dir=eco_dir)
            assert result == []
