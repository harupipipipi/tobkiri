"""
HMACKeyManager のユニットテスト (Wave4-B 改善推奨2)

テスト対象:
  - generate_or_load_signing_key (モジュールレベル関数)
  - compute_data_hmac / verify_data_hmac (モジュールレベル関数)
  - HMACKey (データクラス)
  - HMACKeyManager クラスの主要メソッド
"""
from __future__ import annotations

import base64
import json
import os
import sys
import types
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# ダミーモジュール — hmac_key_manager.py 内の相対インポート回避用
# ---------------------------------------------------------------------------
_PKG = "tobkiri_runtime.core_runtime"

# paths ダミー
_dummy_paths = types.ModuleType(f"{_PKG}.paths")
_dummy_paths.BASE_DIR = Path("/tmp/dummy_base_dir")
sys.modules.setdefault(f"{_PKG}.paths", _dummy_paths)

# audit_logger ダミー
_dummy_audit = types.ModuleType(f"{_PKG}.audit_logger")
_dummy_audit_instance = MagicMock()
_dummy_audit.get_audit_logger = MagicMock(return_value=_dummy_audit_instance)
sys.modules.setdefault(f"{_PKG}.audit_logger", _dummy_audit)

# di_container ダミー
_dummy_di = types.ModuleType(f"{_PKG}.di_container")
_dummy_container = MagicMock()
_dummy_di.get_container = MagicMock(return_value=_dummy_container)
sys.modules.setdefault(f"{_PKG}.di_container", _dummy_di)

# pack_api_server ダミー (core_runtime __init__ が参照する可能性)
_dummy_pack_api = types.ModuleType(f"{_PKG}.pack_api_server")


class _APIResponse:
    def __init__(self, success, data=None, error=None):
        self.success = success
        self.data = data
        self.error = error


_dummy_pack_api.APIResponse = _APIResponse
sys.modules.setdefault(f"{_PKG}.pack_api_server", _dummy_pack_api)

# ---------------------------------------------------------------------------
# テスト対象のインポート
# ---------------------------------------------------------------------------
from tobkiri_runtime.core_runtime.hmac_key_manager import (  # noqa: E402
    HMACKeyManager,
    HMACKey,
    SigningKeyError,
    generate_or_load_signing_key,
    compute_data_hmac,
    verify_data_hmac,
    DEFAULT_GRACE_PERIOD_SECONDS,
)
from tobkiri_runtime.core_runtime import hmac_key_manager as hmac_key_manager_module  # noqa: E402


def _write_owner_key(
    path: Path,
    value: str,
    mode: int = 0o600,
    *,
    secure_windows: bool = True,
) -> None:
    path.write_text(value, encoding="utf-8")
    path.chmod(mode)
    if os.name == "nt" and secure_windows:
        hmac_key_manager_module._secure_windows_signing_key(path)


# ======================================================================
# TestGenerateOrLoadSigningKey
# ======================================================================

class TestGenerateOrLoadSigningKey:
    """generate_or_load_signing_key のテスト"""

    def test_new_key_generated_when_no_file(self, tmp_path):
        """ファイルが存在しない場合、新規生成されること"""
        key_path = tmp_path / "signing.key"
        result = generate_or_load_signing_key(key_path)
        assert isinstance(result, bytes)
        assert len(result) >= 32
        assert key_path.exists()

    def test_existing_key_loaded(self, tmp_path):
        """既存の鍵ファイルからロードされること"""
        key_path = tmp_path / "signing.key"
        stored_key = "a" * 64  # 十分な長さ
        _write_owner_key(key_path, stored_key)
        result = generate_or_load_signing_key(key_path)
        assert result == stored_key.encode("utf-8")

    def test_short_key_rejected(self, tmp_path):
        """Weak existing material is rejected without silent rotation."""
        key_path = tmp_path / "signing.key"
        _write_owner_key(key_path, "short")
        with pytest.raises(SigningKeyError, match="weak"):
            generate_or_load_signing_key(key_path)
        assert key_path.read_text(encoding="utf-8") == "short"

    def test_env_var_takes_priority(self, tmp_path, monkeypatch):
        """Ambient environment injection cannot override Host-owned material."""
        key_path = tmp_path / "signing.key"
        file_key = "file_key_" + "x" * 40
        _write_owner_key(key_path, file_key)
        env_key = "env_key_" + "y" * 40
        monkeypatch.setenv("TEST_SIGNING_KEY", env_key)
        result = generate_or_load_signing_key(key_path, env_var="TEST_SIGNING_KEY")
        assert result == file_key.encode("utf-8")

    def test_env_var_too_short_falls_through(self, tmp_path, monkeypatch):
        """環境変数の鍵長が不十分な場合、ファイルにフォールスルーすること"""
        key_path = tmp_path / "signing.key"
        stored_key = "b" * 64
        _write_owner_key(key_path, stored_key)
        monkeypatch.setenv("TEST_SIGNING_KEY", "short")
        result = generate_or_load_signing_key(key_path, env_var="TEST_SIGNING_KEY")
        assert result == stored_key.encode("utf-8")

    def test_env_var_not_set_falls_through(self, tmp_path):
        """環境変数が未設定の場合、ファイルにフォールスルーすること"""
        key_path = tmp_path / "signing.key"
        stored_key = "c" * 64
        _write_owner_key(key_path, stored_key)
        result = generate_or_load_signing_key(key_path, env_var="NONEXISTENT_VAR")
        assert result == stored_key.encode("utf-8")

    def test_new_key_file_permissions(self, tmp_path):
        """新規生成された鍵ファイルのパーミッションが 0o600 であること (Unix のみ)"""
        key_path = tmp_path / "signing.key"
        generate_or_load_signing_key(key_path)
        if os.name != "nt":
            mode = oct(key_path.stat().st_mode & 0o777)
            assert mode == oct(0o600)

    def test_parent_directory_created(self, tmp_path):
        """親ディレクトリが存在しない場合、自動作成されること"""
        key_path = tmp_path / "subdir" / "deep" / "signing.key"
        result = generate_or_load_signing_key(key_path)
        assert isinstance(result, bytes)
        assert key_path.exists()

    def test_empty_file_rejected(self, tmp_path):
        """Empty existing material fails closed."""
        key_path = tmp_path / "signing.key"
        _write_owner_key(key_path, "")
        with pytest.raises(SigningKeyError, match="weak"):
            generate_or_load_signing_key(key_path)

    def test_symlink_key_rejected(self, tmp_path):
        outside = tmp_path / "outside.key"
        _write_owner_key(outside, "s" * 64)
        key_path = tmp_path / "signing.key"
        key_path.symlink_to(outside)
        with pytest.raises(SigningKeyError, match="symlinked"):
            generate_or_load_signing_key(key_path)

    def test_world_readable_key_rejected(self, tmp_path):
        key_path = tmp_path / "signing.key"
        _write_owner_key(
            key_path,
            "w" * 64,
            mode=0o644,
            secure_windows=False,
        )
        expected = "Windows ACL" if os.name == "nt" else "0600"
        with pytest.raises(SigningKeyError, match=expected):
            generate_or_load_signing_key(key_path)

    def test_restart_loads_same_owner_key(self, tmp_path):
        key_path = tmp_path / "signing.key"
        first = generate_or_load_signing_key(key_path)
        second = generate_or_load_signing_key(key_path)
        assert second == first

    def test_windows_generation_hardens_acl_without_fchmod(
        self, tmp_path, monkeypatch
    ):
        """Windows key creation must use an ACL, not a Unix-only descriptor call."""
        key_path = tmp_path / "signing.key"
        secured: list[Path] = []
        verified: list[Path] = []

        monkeypatch.setattr(hmac_key_manager_module.os, "name", "nt")
        monkeypatch.delattr(hmac_key_manager_module.os, "fchmod", raising=False)
        monkeypatch.setattr(
            hmac_key_manager_module,
            "_secure_windows_signing_key",
            secured.append,
        )
        monkeypatch.setattr(
            hmac_key_manager_module,
            "_verify_windows_signing_key_acl",
            verified.append,
        )

        first = generate_or_load_signing_key(key_path)
        second = generate_or_load_signing_key(key_path)

        assert second == first
        assert len(secured) == 2
        assert secured[-1] == key_path
        assert verified == [key_path]

    def test_windows_acl_script_removes_inheritance_and_verifies_one_sid(
        self, tmp_path, monkeypatch
    ):
        """Windows ACL hardening must be explicit and checked after mutation."""
        calls: list[tuple[list[str], dict[str, object]]] = []

        def _run(argv: list[str], **kwargs: object) -> None:
            calls.append((argv, kwargs))

        key_path = tmp_path / "signing.key"
        monkeypatch.setattr(hmac_key_manager_module.os, "name", "nt")
        monkeypatch.setenv(
            hmac_key_manager_module._WINDOWS_SIGNING_KEY_ACL_TARGET_ENV,
            "untrusted-parent-value",
        )
        monkeypatch.setattr(hmac_key_manager_module.subprocess, "run", _run)
        hmac_key_manager_module._secure_windows_signing_key(key_path)

        assert len(calls) == 1
        argv, kwargs = calls[0]
        assert argv[:5] == [
            "powershell.exe",
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
        ]
        assert str(tmp_path) not in argv[-1]
        assert "FileSecurity]::new()" in argv[-1]
        assert "SetAccessRuleProtection($true, $false)" in argv[-1]
        assert "$rules.Count -ne 1" in argv[-1]
        assert "$rule.IsInherited" in argv[-1]
        assert "[Console]::In.ReadToEnd()" not in argv[-1]
        assert "$env:TOBKIRI_SIGNING_KEY_ACL_TARGET_B64" in argv[-1]
        environment = kwargs.pop("env")
        assert environment[
            hmac_key_manager_module._WINDOWS_SIGNING_KEY_ACL_TARGET_ENV
        ] == base64.b64encode(str(key_path).encode("utf-8")).decode("ascii")
        assert kwargs == {
            "check": True,
            "capture_output": True,
            "text": True,
            "timeout": hmac_key_manager_module._WINDOWS_SIGNING_KEY_ACL_TIMEOUT_SECONDS,
        }

    @pytest.mark.parametrize(
        ("harden", "expected_message"),
        (
            (True, "could not be secured"),
            (False, "is unsafe"),
        ),
    )
    def test_windows_acl_timeout_fails_closed(
        self, tmp_path, monkeypatch, harden, expected_message
    ):
        """A bounded Windows ACL timeout must never permit an unsafe key."""
        key_path = tmp_path / "signing.key"

        def _run(argv: list[str], **kwargs: object) -> None:
            assert kwargs["timeout"] == (
                hmac_key_manager_module._WINDOWS_SIGNING_KEY_ACL_TIMEOUT_SECONDS
            )
            raise hmac_key_manager_module.subprocess.TimeoutExpired(argv, 60)

        monkeypatch.setattr(hmac_key_manager_module.subprocess, "run", _run)

        with pytest.raises(SigningKeyError, match=expected_message):
            hmac_key_manager_module._run_windows_signing_key_acl(key_path, harden=harden)

    @pytest.mark.parametrize(
        "filename",
        (
            "署名鍵_日本語_e\u0301_é_🙂.key",
            " leading-whitespace.key",
            "trailing-whitespace.key ",
            "\tleading-control.key",
            "trailing-control.key\r\n",
        ),
    )
    def test_windows_acl_path_transport_preserves_exact_code_points(
        self, tmp_path, monkeypatch, filename
    ):
        """Harden and verify must receive the same unnormalized Unicode path."""
        calls: list[tuple[list[str], dict[str, object]]] = []

        def _run(argv: list[str], **kwargs: object) -> None:
            calls.append((argv, kwargs))

        key_path = tmp_path / filename
        expected_path = str(key_path)
        expected_code_points = tuple(map(ord, expected_path))
        monkeypatch.setattr(hmac_key_manager_module.subprocess, "run", _run)

        hmac_key_manager_module._secure_windows_signing_key(key_path)
        hmac_key_manager_module._verify_windows_signing_key_acl(key_path)

        assert len(calls) == 2
        assert "input" not in calls[0][1]
        assert "input" not in calls[1][1]
        target_env = hmac_key_manager_module._WINDOWS_SIGNING_KEY_ACL_TARGET_ENV
        assert calls[0][1]["env"][target_env] == calls[1][1]["env"][target_env]
        for argv, kwargs in calls:
            command = argv[-1]
            assert expected_path not in command
            assert ".Trim()" not in command
            assert "[Console]::In.ReadToEnd()" not in command
            assert "[Convert]::FromBase64String($encodedTarget)" in command
            assert "[System.Text.UTF8Encoding]::new($false, $true)" in command
            payload = kwargs["env"][target_env]
            assert isinstance(payload, str)
            decoded_path = base64.b64decode(payload, validate=True).decode("utf-8")
            assert tuple(map(ord, decoded_path)) == expected_code_points


# ======================================================================
# TestComputeDataHmac
# ======================================================================

class TestComputeDataHmac:
    """compute_data_hmac のテスト"""

    def test_basic_signature(self):
        """基本的な署名計算"""
        key = b"test_key_for_hmac"
        data = {"name": "Alice", "score": 100}
        sig = compute_data_hmac(key, data)
        assert isinstance(sig, str)
        assert len(sig) == 64  # SHA-256 hex = 64文字

    def test_deterministic(self):
        """同じデータ・鍵で同じ署名が生成されること"""
        key = b"deterministic_key"
        data = {"x": 1, "y": 2}
        sig1 = compute_data_hmac(key, data)
        sig2 = compute_data_hmac(key, data)
        assert sig1 == sig2

    def test_key_order_independent(self):
        """辞書キーの順序に依存しないこと（sort_keys=True）"""
        key = b"order_key"
        data1 = {"b": 2, "a": 1}
        data2 = {"a": 1, "b": 2}
        assert compute_data_hmac(key, data1) == compute_data_hmac(key, data2)

    def test_hmac_prefix_keys_excluded(self):
        """_hmac で始まるキーが除外されること"""
        key = b"exclude_key"
        data_without = {"name": "Bob"}
        data_with = {"name": "Bob", "_hmac_sig": "old_sig", "_hmac_ts": "12345"}
        assert compute_data_hmac(key, data_without) == compute_data_hmac(key, data_with)

    def test_different_data_different_sig(self):
        """異なるデータでは異なる署名になること"""
        key = b"diff_key"
        sig1 = compute_data_hmac(key, {"a": 1})
        sig2 = compute_data_hmac(key, {"a": 2})
        assert sig1 != sig2

    def test_different_key_different_sig(self):
        """異なる鍵では異なる署名になること"""
        data = {"a": 1}
        sig1 = compute_data_hmac(b"key_one", data)
        sig2 = compute_data_hmac(b"key_two", data)
        assert sig1 != sig2


# ======================================================================
# TestVerifyDataHmac
# ======================================================================

class TestVerifyDataHmac:
    """verify_data_hmac のテスト"""

    def test_roundtrip(self):
        """署名→検証のラウンドトリップが成功すること"""
        key = b"roundtrip_key_for_testing"
        data = {"user": "Alice", "action": "login"}
        sig = compute_data_hmac(key, data)
        assert verify_data_hmac(key, data, sig) is True

    def test_tampered_data_rejected(self):
        """改ざんされたデータが拒否されること"""
        key = b"tamper_key_for_testing"
        data = {"user": "Alice", "action": "login"}
        sig = compute_data_hmac(key, data)
        tampered = {"user": "Eve", "action": "login"}
        assert verify_data_hmac(key, tampered, sig) is False

    def test_wrong_key_rejected(self):
        """異なる鍵で検証すると拒否されること"""
        key1 = b"key_one_for_test"
        key2 = b"key_two_for_test"
        data = {"msg": "hello"}
        sig = compute_data_hmac(key1, data)
        assert verify_data_hmac(key2, data, sig) is False

    def test_wrong_hmac_string_rejected(self):
        """不正な HMAC 文字列が拒否されること"""
        key = b"reject_key_for_testing"
        data = {"msg": "hello"}
        assert verify_data_hmac(key, data, "0" * 64) is False
        assert verify_data_hmac(key, data, "") is False

    def test_hmac_keys_ignored_in_verification(self):
        """_hmac キーが含まれていても検証が成功すること"""
        key = b"ignore_key_for_testing"
        data = {"user": "Bob"}
        sig = compute_data_hmac(key, data)
        data_with_hmac = {"user": "Bob", "_hmac_sig": sig}
        assert verify_data_hmac(key, data_with_hmac, sig) is True


# ======================================================================
# TestHMACKey
# ======================================================================

class TestHMACKey:
    """HMACKey データクラスのテスト"""

    def test_to_dict_roundtrip(self):
        """to_dict → from_dict のラウンドトリップ"""
        key = HMACKey(
            key="test_key_value",
            created_at="2025-01-01T00:00:00Z",
            rotated_at=None,
            is_active=True,
        )
        d = key.to_dict()
        restored = HMACKey.from_dict(d)
        assert restored.key == key.key
        assert restored.created_at == key.created_at
        assert restored.rotated_at is None
        assert restored.is_active is True

    def test_from_dict_with_rotated(self):
        """退役済み鍵の from_dict"""
        d = {
            "key": "old_key",
            "created_at": "2025-01-01T00:00:00Z",
            "rotated_at": "2025-01-02T00:00:00Z",
            "is_active": False,
        }
        key = HMACKey.from_dict(d)
        assert key.is_active is False
        assert key.rotated_at == "2025-01-02T00:00:00Z"

    def test_from_dict_defaults(self):
        """from_dict でオプションフィールドのデフォルト値"""
        d = {"key": "k", "created_at": "2025-01-01T00:00:00Z"}
        key = HMACKey.from_dict(d)
        assert key.rotated_at is None
        assert key.is_active is True


# ======================================================================
# TestHMACKeyManagerInit
# ======================================================================

class TestHMACKeyManagerInit:
    """HMACKeyManager 初期化のテスト"""

    def test_default_path_uses_launcher_user_data(self, tmp_path, monkeypatch):
        user_data = tmp_path / "launcher-user-data"
        monkeypatch.setenv("RUMI_USER_DATA", str(user_data))

        mgr = HMACKeyManager()

        assert mgr._keys_path == user_data / "hmac_keys.json"

    def test_first_boot_generates_key(self, tmp_path):
        """鍵ファイルが存在しない場合、初回起動で鍵が生成されること"""
        keys_path = str(tmp_path / "hmac_keys.json")
        mgr = HMACKeyManager(keys_path=keys_path)
        assert Path(keys_path).exists()
        key = mgr.get_active_key()
        assert isinstance(key, str)
        assert len(key) > 0

    def test_reload_existing_keys(self, tmp_path):
        """既存の鍵ファイルからリロードされること"""
        keys_path = str(tmp_path / "hmac_keys.json")
        mgr1 = HMACKeyManager(keys_path=keys_path)
        key1 = mgr1.get_active_key()

        # 同じパスで再生成 — 同じ鍵がロードされること
        mgr2 = HMACKeyManager(keys_path=keys_path)
        key2 = mgr2.get_active_key()
        assert key1 == key2

    def test_corrupted_file_recovery(self, tmp_path):
        """壊れた鍵ファイルからリカバリーされること"""
        keys_path = tmp_path / "hmac_keys.json"
        keys_path.write_text("{invalid json!!!}", encoding="utf-8")
        mgr = HMACKeyManager(keys_path=str(keys_path))
        key = mgr.get_active_key()
        assert isinstance(key, str)
        assert len(key) > 0
        # バックアップファイルが作成されること
        bak_path = keys_path.with_suffix(".json.bak")
        assert bak_path.exists()

    def test_env_rotate_trigger(self, tmp_path, monkeypatch):
        """Ambient rotation injection is ignored; explicit rotate remains required."""
        keys_path = str(tmp_path / "hmac_keys.json")
        mgr1 = HMACKeyManager(keys_path=keys_path)
        key1 = mgr1.get_active_key()

        monkeypatch.setenv("RUMI_HMAC_ROTATE", "true")
        mgr2 = HMACKeyManager(keys_path=keys_path)
        key2 = mgr2.get_active_key()
        assert key1 == key2
        assert os.environ.get("RUMI_HMAC_ROTATE") == "true"

    def test_grace_period_custom(self, tmp_path):
        """カスタムグレースピリオドが設定されること"""
        keys_path = str(tmp_path / "hmac_keys.json")
        mgr = HMACKeyManager(keys_path=keys_path, grace_period_seconds=3600)
        info = mgr.get_key_info()
        assert info["grace_period_seconds"] == 3600


# ======================================================================
# TestGetActiveKey
# ======================================================================

class TestGetActiveKey:
    """get_active_key のテスト"""

    def test_returns_string(self, tmp_path):
        """アクティブ鍵が文字列で返されること"""
        mgr = HMACKeyManager(keys_path=str(tmp_path / "k.json"))
        key = mgr.get_active_key()
        assert isinstance(key, str)
        assert len(key) > 0

    def test_consistent_on_repeated_calls(self, tmp_path):
        """連続呼び出しで同じ鍵が返されること"""
        mgr = HMACKeyManager(keys_path=str(tmp_path / "k.json"))
        k1 = mgr.get_active_key()
        k2 = mgr.get_active_key()
        assert k1 == k2


# ======================================================================
# TestVerifyToken
# ======================================================================

class TestVerifyToken:
    """verify_token のテスト"""

    def test_valid_token_accepted(self, tmp_path):
        """アクティブ鍵と一致するトークンが受理されること"""
        mgr = HMACKeyManager(keys_path=str(tmp_path / "k.json"))
        token = mgr.get_active_key()
        assert mgr.verify_token(token) is True

    def test_invalid_token_rejected(self, tmp_path):
        """一致しないトークンが拒否されること"""
        mgr = HMACKeyManager(keys_path=str(tmp_path / "k.json"))
        assert mgr.verify_token("invalid_token_string") is False

    def test_empty_token_rejected(self, tmp_path):
        """空トークンが拒否されること"""
        mgr = HMACKeyManager(keys_path=str(tmp_path / "k.json"))
        assert mgr.verify_token("") is False

    def test_one_char_diff_rejected(self, tmp_path):
        """1文字違いのトークンが拒否されること"""
        mgr = HMACKeyManager(keys_path=str(tmp_path / "k.json"))
        token = mgr.get_active_key()
        wrong = token[:-1] + ("X" if token[-1] != "X" else "Y")
        assert mgr.verify_token(wrong) is False


# ======================================================================
# TestRotate
# ======================================================================

class TestRotate:
    """rotate / rotate_key のテスト"""

    def test_rotate_returns_new_key(self, tmp_path):
        """ローテーション後に新しい鍵が返されること"""
        mgr = HMACKeyManager(keys_path=str(tmp_path / "k.json"))
        old_key = mgr.get_active_key()
        new_key = mgr.rotate()
        assert new_key != old_key
        assert mgr.get_active_key() == new_key

    def test_old_key_valid_during_grace_period(self, tmp_path):
        """グレースピリオド内は旧鍵でも verify_token が成功すること"""
        mgr = HMACKeyManager(
            keys_path=str(tmp_path / "k.json"),
            grace_period_seconds=3600,
        )
        old_key = mgr.get_active_key()
        mgr.rotate()
        # 旧鍵がまだ有効
        assert mgr.verify_token(old_key) is True

    def test_old_key_rejected_after_grace_period(self, tmp_path):
        """グレースピリオド超過後は旧鍵が拒否されること"""
        mgr = HMACKeyManager(
            keys_path=str(tmp_path / "k.json"),
            grace_period_seconds=1,  # 1秒
        )
        old_key = mgr.get_active_key()

        # rotate で旧鍵を退役
        mgr.rotate()

        # 時間を進める: _now_utc をモックして未来にする
        future_time = datetime.now(timezone.utc) + timedelta(seconds=10)
        with patch(
            "tobkiri_runtime.core_runtime.hmac_key_manager._now_utc",
            return_value=future_time,
        ):
            # cleanup を強制トリガー: 再ロード
            mgr._load_or_initialize()
            assert mgr.verify_token(old_key) is False

    def test_old_key_rejected_after_grace_period_without_reload(self, tmp_path):
        """再ロードなしでもグレースピリオド超過後の旧鍵が拒否されること"""
        mgr = HMACKeyManager(
            keys_path=str(tmp_path / "k.json"),
            grace_period_seconds=1,
        )
        old_key = mgr.get_active_key()
        new_key = mgr.rotate()

        future_time = datetime.now(timezone.utc) + timedelta(seconds=10)
        with patch(
            "tobkiri_runtime.core_runtime.hmac_key_manager._now_utc",
            return_value=future_time,
        ):
            assert mgr.verify_token(old_key) is False
            assert mgr.verify_token(new_key) is True

    def test_rotate_key_alias(self, tmp_path):
        """rotate_key が rotate のエイリアスであること"""
        mgr = HMACKeyManager(keys_path=str(tmp_path / "k.json"))
        assert mgr.rotate_key.__func__ is mgr.rotate.__func__

    def test_multiple_rotations(self, tmp_path):
        """複数回ローテーションしても正常に動作すること"""
        mgr = HMACKeyManager(
            keys_path=str(tmp_path / "k.json"),
            grace_period_seconds=3600,
        )
        keys = [mgr.get_active_key()]
        for _ in range(5):
            keys.append(mgr.rotate())
        # 最新の鍵がアクティブ
        assert mgr.get_active_key() == keys[-1]
        # 全ての旧鍵がグレースピリオド内で有効
        for k in keys:
            assert mgr.verify_token(k) is True

    def test_rotate_persists_to_file(self, tmp_path):
        """ローテーション結果がファイルに永続化されること"""
        keys_path = str(tmp_path / "k.json")
        mgr = HMACKeyManager(keys_path=keys_path)
        new_key = mgr.rotate()

        # 別インスタンスで再ロード
        mgr2 = HMACKeyManager(keys_path=keys_path)
        assert mgr2.get_active_key() == new_key


# ======================================================================
# TestGetKeyInfo
# ======================================================================

class TestGetKeyInfo:
    """get_key_info のテスト"""

    def test_structure(self, tmp_path):
        """返り値の構造が正しいこと"""
        mgr = HMACKeyManager(keys_path=str(tmp_path / "k.json"))
        info = mgr.get_key_info()
        assert "total_keys" in info
        assert "active_keys" in info
        assert "grace_period_keys" in info
        assert "grace_period_seconds" in info
        assert "keys_path" in info
        assert "keys" in info
        assert isinstance(info["keys"], list)

    def test_key_prefix_exposed(self, tmp_path):
        """鍵のプレフィックス（先頭8文字 + ...）のみが露出すること"""
        mgr = HMACKeyManager(keys_path=str(tmp_path / "k.json"))
        info = mgr.get_key_info()
        for key_info in info["keys"]:
            assert key_info["key_prefix"].endswith("...")
            # フルキーが含まれていないこと (8 + "..." = 11)
            assert len(key_info["key_prefix"]) == 11

    def test_counts_after_rotation(self, tmp_path):
        """ローテーション後のカウントが正しいこと"""
        mgr = HMACKeyManager(keys_path=str(tmp_path / "k.json"))
        mgr.rotate()
        info = mgr.get_key_info()
        assert info["active_keys"] == 1
        assert info["grace_period_keys"] >= 1
        assert info["total_keys"] >= 2


# ======================================================================
# TestSecurityProperties
# ======================================================================

class TestSecurityProperties:
    """セキュリティ特性のテスト"""

    def test_hmac_verify_correctness(self):
        """verify_data_hmac が正確に動作すること（正誤判定の精度）"""
        key = b"timing_test_key_for_test"
        data = {"test": "data"}
        sig = compute_data_hmac(key, data)
        # 正しい署名
        assert verify_data_hmac(key, data, sig) is True
        # 不正な署名（完全に異なる）
        assert verify_data_hmac(key, data, "wrong" * 13) is False
        # 不正な署名（1文字違い）
        assert verify_data_hmac(key, data, sig[:-1] + "0") is False

    def test_key_file_atomic_write(self, tmp_path):
        """鍵ファイルが正常な JSON であること（atomic write の間接確認）"""
        keys_path = str(tmp_path / "k.json")
        mgr = HMACKeyManager(keys_path=keys_path)
        mgr.rotate()
        mgr.rotate()
        with open(keys_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        assert "version" in data
        if data.get("encryption") == "fernet":
            assert "payload" in data
        else:
            assert "keys" in data
            assert isinstance(data["keys"], list)

    def test_default_grace_period(self, tmp_path):
        """デフォルトグレースピリオドが 86400 秒であること"""
        assert DEFAULT_GRACE_PERIOD_SECONDS == 86400
        keys_path = str(tmp_path / "k.json")
        mgr = HMACKeyManager(keys_path=keys_path)
        info = mgr.get_key_info()
        assert info["grace_period_seconds"] == 86400
