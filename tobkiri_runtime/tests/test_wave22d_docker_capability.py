"""Tests for DockerCapabilityHandler (W22-D).

subprocess.run を mock して Docker が無い環境でも全テスト実行可能。
"""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from core_runtime.docker_capability import DockerCapabilityHandler


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def handler() -> DockerCapabilityHandler:
    """新しい DockerCapabilityHandler インスタンスを返す。"""
    return DockerCapabilityHandler()


@pytest.fixture
def base_grant() -> dict:
    """基本的な grant_config を返す。"""
    return {
        "allowed_images": ["python:3.*-slim", "node:*-alpine"],
        "max_memory": "512m",
        "max_cpus": "1.0",
        "max_pids": 100,
        "network_allowed": False,
        "max_containers": 3,
        "max_execution_time": 120,
    }


@pytest.fixture
def base_args() -> dict:
    """基本的な args を返す。"""
    return {
        "image": "python:3.11-slim",
        "command": ["python", "-c", "print('hello')"],
    }


def _mock_completed(
    returncode: int = 0, stdout: str = "ok", stderr: str = ""
) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr
    )


# ---------------------------------------------------------------------------
# イメージホワイトリスト
# ---------------------------------------------------------------------------

class TestImageWhitelist:
    """イメージ許可チェックのテスト。"""

    @patch("core_runtime.docker_capability.subprocess.run",
           return_value=_mock_completed())
    def test_image_allowed(
        self, mock_run: MagicMock,
        handler: DockerCapabilityHandler,
        base_grant: dict, base_args: dict,
    ) -> None:
        """許可イメージで成功する。"""
        result = handler.handle_run("pack-001", base_args, base_grant)
        assert "error" not in result
        assert result["exit_code"] == 0
        mock_run.assert_called_once()

    @patch("core_runtime.docker_capability.subprocess.run",
           return_value=_mock_completed())
    def test_image_rejected(
        self, mock_run: MagicMock,
        handler: DockerCapabilityHandler, base_grant: dict,
    ) -> None:
        """拒否イメージでエラーになる。"""
        args = {"image": "ubuntu:latest", "command": ["echo", "hi"]}
        result = handler.handle_run("pack-001", args, base_grant)
        assert "error" in result
        assert "not allowed" in result["error"]
        mock_run.assert_not_called()

    @patch("core_runtime.docker_capability.subprocess.run",
           return_value=_mock_completed())
    def test_image_glob_pattern(
        self, mock_run: MagicMock,
        handler: DockerCapabilityHandler, base_grant: dict,
    ) -> None:
        """python:3.*-slim が python:3.11-slim にマッチする。"""
        args = {"image": "python:3.11-slim", "command": ["python", "--version"]}
        result = handler.handle_run("pack-001", args, base_grant)
        assert "error" not in result
        mock_run.assert_called_once()

    @patch("core_runtime.docker_capability.subprocess.run",
           return_value=_mock_completed())
    def test_allowed_images_empty(
        self, mock_run: MagicMock, handler: DockerCapabilityHandler,
    ) -> None:
        """allowed_images が空リストのとき全拒否する。"""
        grant = {"allowed_images": []}
        args = {"image": "python:3.11-slim", "command": ["echo"]}
        result = handler.handle_run("pack-001", args, grant)
        assert "error" in result
        assert "not allowed" in result["error"]
        mock_run.assert_not_called()

    @patch("core_runtime.docker_capability.subprocess.run",
           return_value=_mock_completed())
    def test_allowed_images_missing(
        self, mock_run: MagicMock, handler: DockerCapabilityHandler,
    ) -> None:
        """allowed_images キー自体がない場合も全拒否する。"""
        grant: dict = {}
        args = {"image": "python:3.11-slim", "command": ["echo"]}
        result = handler.handle_run("pack-001", args, grant)
        assert "error" in result
        mock_run.assert_not_called()

    @patch("core_runtime.docker_capability.subprocess.run",
           return_value=_mock_completed())
    def test_option_like_image_rejected_before_allowlist(
        self, mock_run: MagicMock, handler: DockerCapabilityHandler,
    ) -> None:
        """'*' grant でも Docker option に見える image は拒否する。"""
        grant = {"allowed_images": ["*"]}
        args = {
            "image": "--user=0:0",
            "command": [
                "--privileged",
                "--network=host",
                "-v",
                "/:/host:rw",
                "alpine:latest",
                "sh",
            ],
        }
        result = handler.handle_run("pack-001", args, grant)
        assert "error" in result
        assert "Invalid image reference" in result["error"]
        mock_run.assert_not_called()

    @patch("core_runtime.docker_capability.subprocess.run",
           return_value=_mock_completed())
    def test_valid_registry_image_allowed_with_wildcard(
        self, mock_run: MagicMock, handler: DockerCapabilityHandler,
    ) -> None:
        """registry/namespace/tag 付きの正規 image は従来通り許可する。"""
        grant = {"allowed_images": ["registry.example.com/team/*"]}
        args = {
            "image": "registry.example.com/team/app:2026.06",
            "command": ["echo", "ok"],
        }
        result = handler.handle_run("pack-001", args, grant)
        assert "error" not in result
        mock_run.assert_called_once()


# ---------------------------------------------------------------------------
# メモリ制限
# ---------------------------------------------------------------------------

class TestMemoryLimits:
    """メモリ制限のテスト。"""

    @patch("core_runtime.docker_capability.subprocess.run",
           return_value=_mock_completed())
    def test_memory_within_grant(
        self, mock_run: MagicMock,
        handler: DockerCapabilityHandler,
        base_grant: dict, base_args: dict,
    ) -> None:
        """args の memory が grant の max_memory 以下なら使用される。"""
        base_args["memory"] = "256m"
        handler.handle_run("pack-001", base_args, base_grant)
        cmd = mock_run.call_args[0][0]
        assert "--memory=256m" in cmd

    @patch("core_runtime.docker_capability.subprocess.run",
           return_value=_mock_completed())
    def test_memory_exceeds_grant(
        self, mock_run: MagicMock,
        handler: DockerCapabilityHandler,
        base_grant: dict, base_args: dict,
    ) -> None:
        """args の memory が grant の max_memory を超える → grant 上限に制限。"""
        base_args["memory"] = "1g"
        base_grant["max_memory"] = "512m"
        handler.handle_run("pack-001", base_args, base_grant)
        cmd = mock_run.call_args[0][0]
        assert "--memory=512m" in cmd

    @patch("core_runtime.docker_capability.subprocess.run",
           return_value=_mock_completed())
    def test_memory_grant_exceeds_absolute(
        self, mock_run: MagicMock,
        handler: DockerCapabilityHandler,
        base_grant: dict, base_args: dict,
    ) -> None:
        """grant の max_memory が絶対上限 (1g) 超過 → 絶対上限に制限。"""
        base_grant["max_memory"] = "2g"
        base_args["memory"] = "2g"
        handler.handle_run("pack-001", base_args, base_grant)
        cmd = mock_run.call_args[0][0]
        assert "--memory=1g" in cmd


# ---------------------------------------------------------------------------
# 環境変数フィルタ
# ---------------------------------------------------------------------------

class TestEnvFilter:
    """環境変数フィルタのテスト。"""

    @patch("core_runtime.docker_capability.subprocess.run",
           return_value=_mock_completed())
    def test_env_filter_rumi(
        self, mock_run: MagicMock,
        handler: DockerCapabilityHandler,
        base_grant: dict, base_args: dict,
    ) -> None:
        """RUMI_* が除去される。"""
        base_args["env"] = {"RUMI_SECRET": "x", "MY_VAR": "y"}
        handler.handle_run("pack-001", base_args, base_grant)
        cmd_str = " ".join(mock_run.call_args[0][0])
        assert "RUMI_SECRET" not in cmd_str
        assert "MY_VAR=y" in cmd_str

    @patch("core_runtime.docker_capability.subprocess.run",
           return_value=_mock_completed())
    def test_env_filter_normal(
        self, mock_run: MagicMock,
        handler: DockerCapabilityHandler,
        base_grant: dict, base_args: dict,
    ) -> None:
        """通常の環境変数は通る。"""
        base_args["env"] = {"APP_MODE": "prod", "DEBUG": "0"}
        handler.handle_run("pack-001", base_args, base_grant)
        cmd_str = " ".join(mock_run.call_args[0][0])
        assert "APP_MODE=prod" in cmd_str
        assert "DEBUG=0" in cmd_str

    @patch("core_runtime.docker_capability.subprocess.run",
           return_value=_mock_completed())
    def test_env_filter_aws(
        self, mock_run: MagicMock,
        handler: DockerCapabilityHandler,
        base_grant: dict, base_args: dict,
    ) -> None:
        """AWS_* が除去される。"""
        base_args["env"] = {"AWS_SECRET_KEY": "x", "SAFE": "y"}
        handler.handle_run("pack-001", base_args, base_grant)
        cmd_str = " ".join(mock_run.call_args[0][0])
        assert "AWS_SECRET_KEY" not in cmd_str
        assert "SAFE=y" in cmd_str

    @patch("core_runtime.docker_capability.subprocess.run",
           return_value=_mock_completed())
    def test_env_filter_docker(
        self, mock_run: MagicMock,
        handler: DockerCapabilityHandler,
        base_grant: dict, base_args: dict,
    ) -> None:
        """DOCKER_* が除去される。"""
        base_args["env"] = {"DOCKER_HOST": "x", "SAFE": "y"}
        handler.handle_run("pack-001", base_args, base_grant)
        cmd_str = " ".join(mock_run.call_args[0][0])
        assert "DOCKER_HOST" not in cmd_str

    @patch("core_runtime.docker_capability.subprocess.run",
           return_value=_mock_completed())
    def test_env_filter_home_path(
        self, mock_run: MagicMock,
        handler: DockerCapabilityHandler,
        base_grant: dict, base_args: dict,
    ) -> None:
        """HOME と PATH が除去される。"""
        base_args["env"] = {"HOME": "/root", "PATH": "/usr/bin", "OK": "1"}
        handler.handle_run("pack-001", base_args, base_grant)
        cmd_str = " ".join(mock_run.call_args[0][0])
        assert "HOME=/root" not in cmd_str
        assert "PATH=/usr/bin" not in cmd_str
        assert "OK=1" in cmd_str


# ---------------------------------------------------------------------------
# 同時コンテナ数
# ---------------------------------------------------------------------------

class TestContainerLimit:
    """同時コンテナ数制限のテスト。"""

    @patch("core_runtime.docker_capability.subprocess.run",
           return_value=_mock_completed())
    def test_containers_under_limit(
        self, mock_run: MagicMock,
        handler: DockerCapabilityHandler,
        base_grant: dict, base_args: dict,
    ) -> None:
        """上限未満なら成功する。"""
        base_grant["max_containers"] = 3
        result = handler.handle_run("pack-001", base_args, base_grant)
        assert "error" not in result

    @patch("core_runtime.docker_capability.subprocess.run",
           return_value=_mock_completed())
    def test_containers_at_limit(
        self, mock_run: MagicMock,
        handler: DockerCapabilityHandler,
        base_grant: dict, base_args: dict,
    ) -> None:
        """上限到達でエラーになる。"""
        base_grant["max_containers"] = 1
        with handler._lock:
            handler._active_containers["fake-container-1"] = "pack-001"
        result = handler.handle_run("pack-001", base_args, base_grant)
        assert "error" in result
        assert "limit" in result["error"].lower()
        mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# タイムアウト
# ---------------------------------------------------------------------------

class TestTimeout:
    """タイムアウトのテスト。"""

    @patch("core_runtime.docker_capability.subprocess.run",
           return_value=_mock_completed())
    def test_timeout_applied(
        self, mock_run: MagicMock,
        handler: DockerCapabilityHandler,
        base_grant: dict, base_args: dict,
    ) -> None:
        """指定した timeout で実行される。"""
        base_args["timeout"] = 30
        handler.handle_run("pack-001", base_args, base_grant)
        call_kwargs = mock_run.call_args[1]
        assert call_kwargs["timeout"] == 30


# ---------------------------------------------------------------------------
# ネットワーク
# ---------------------------------------------------------------------------

class TestNetwork:
    """ネットワーク設定のテスト。"""

    @patch("core_runtime.docker_capability.subprocess.run",
           return_value=_mock_completed())
    def test_network_disabled(
        self, mock_run: MagicMock,
        handler: DockerCapabilityHandler,
        base_grant: dict, base_args: dict,
    ) -> None:
        """network_allowed=false → --network=none。"""
        base_grant["network_allowed"] = False
        handler.handle_run("pack-001", base_args, base_grant)
        cmd = mock_run.call_args[0][0]
        assert "--network=none" in cmd

    @patch("core_runtime.docker_capability.subprocess.run",
           return_value=_mock_completed())
    def test_network_enabled(
        self, mock_run: MagicMock,
        handler: DockerCapabilityHandler,
        base_grant: dict, base_args: dict,
    ) -> None:
        """network_allowed=true → bridge (host は使わない)。"""
        base_grant["network_allowed"] = True
        handler.handle_run("pack-001", base_args, base_grant)
        cmd = mock_run.call_args[0][0]
        assert "--network=bridge" in cmd
        assert "--network=host" not in cmd


# ---------------------------------------------------------------------------
# 監査ログ
# ---------------------------------------------------------------------------

class TestAuditLog:
    """監査ログのテスト。"""

    @patch("core_runtime.docker_capability.subprocess.run",
           return_value=_mock_completed())
    def test_audit_logged(
        self, mock_run: MagicMock,
        handler: DockerCapabilityHandler,
        base_grant: dict, base_args: dict,
    ) -> None:
        """監査ログが記録される（mock で検証）。"""
        mock_audit = MagicMock()
        mock_audit._now_ts = MagicMock(return_value="2026-01-01T00:00:00Z")

        with patch("core_runtime.di_container.get_container") as mock_gc:
            mock_container = MagicMock()
            mock_container.get_or_none.return_value = mock_audit
            mock_gc.return_value = mock_container

            handler.handle_run("pack-001", base_args, base_grant)
            assert mock_audit.log.called


# ---------------------------------------------------------------------------
# DI 登録
# ---------------------------------------------------------------------------

class TestDIRegistration:
    """退役済みDocker executorがDIへ再登録されないことを検証する。"""

    def test_di_registration(self) -> None:
        """docker_capability_handler は v4 DI コンテナに登録されない。"""
        from core_runtime.di_container import get_container, reset_container

        reset_container()
        try:
            c = get_container()
            assert "docker_capability_handler" not in c.registered_names()
            assert c.get_or_none("docker_capability_handler") is None
        finally:
            reset_container()
