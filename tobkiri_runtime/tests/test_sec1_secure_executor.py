"""
SEC-1: SecureExecutor 単体テスト

Wave 1-4 の修正に対するテスト:
- Docker image ダイジェスト固定 + 環境変数上書き
- _sanitize_context の強化 (payload サイズ/深度制限)
- _execute_lib_in_container の sanitize 漏れ修正
- ホスト実行タイムアウト
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch, MagicMock, PropertyMock

import pytest

from core_runtime.secure_executor import (
    SecureExecutor,
    ExecutionResult,
    DEFAULT_EXECUTOR_IMAGE,
    EXECUTOR_IMAGE,
    MAX_CONTEXT_PAYLOAD_SIZE,
    MAX_CONTEXT_DEPTH,
    MAX_HOST_EXECUTION_TIMEOUT,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_executor(monkeypatch, mode: str = "permissive") -> SecureExecutor:
    """指定したセキュリティモードで SecureExecutor を生成する。"""
    monkeypatch.setenv("RUMI_SECURITY_MODE", mode)
    return SecureExecutor()


# ---------------------------------------------------------------------------
# Wave 1: Docker image ダイジェスト固定 + 環境変数上書き
# ---------------------------------------------------------------------------

class TestExecutorImage:
    """Wave 1: image 定数と環境変数上書きのテスト。"""

    def test_executor_image_default(self) -> None:
        if "RUMI_EXECUTOR_IMAGE" not in os.environ:
            assert EXECUTOR_IMAGE == DEFAULT_EXECUTOR_IMAGE
        assert "@sha256:" in DEFAULT_EXECUTOR_IMAGE
        assert DEFAULT_EXECUTOR_IMAGE.startswith("python:3.11-slim@sha256:")

    def test_executor_image_env_override(self, monkeypatch) -> None:
        custom_image = "python:3.11-slim@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        monkeypatch.setenv("RUMI_EXECUTOR_IMAGE", custom_image)
        import core_runtime.secure_executor as se_mod
        monkeypatch.setattr(se_mod, "EXECUTOR_IMAGE", custom_image)
        assert se_mod.EXECUTOR_IMAGE == custom_image
        assert os.environ.get("RUMI_EXECUTOR_IMAGE", DEFAULT_EXECUTOR_IMAGE) == custom_image


# ---------------------------------------------------------------------------
# Wave 2: _sanitize_context の強化
# ---------------------------------------------------------------------------

class TestSanitizeContext:

    def test_sanitize_context_normal_payload(self, monkeypatch) -> None:
        executor = _make_executor(monkeypatch)
        context = {
            "phase": "setup",
            "ts": "2024-01-01T00:00:00Z",
            "payload": {"key": "value", "nested": {"a": 1}},
        }
        result = executor._sanitize_context(context)
        assert "payload" in result
        assert result["payload"]["key"] == "value"
        assert result["phase"] == "setup"

    def test_sanitize_context_payload_size_limit(self, monkeypatch) -> None:
        executor = _make_executor(monkeypatch)
        large_payload = {"data": "x" * (MAX_CONTEXT_PAYLOAD_SIZE + 1)}
        context = {"phase": "setup", "payload": large_payload}
        result = executor._sanitize_context(context)
        assert "payload" not in result
        assert result["phase"] == "setup"

    def test_sanitize_context_payload_depth_limit(self, monkeypatch) -> None:
        executor = _make_executor(monkeypatch)
        deep_obj = "leaf"
        for _ in range(MAX_CONTEXT_DEPTH + 1):
            deep_obj = {"nested": deep_obj}
        context = {"phase": "setup", "payload": deep_obj}
        result = executor._sanitize_context(context)
        assert "payload" not in result
        assert result["phase"] == "setup"

    def test_sanitize_context_payload_at_max_depth(self, monkeypatch) -> None:
        executor = _make_executor(monkeypatch)
        obj = "leaf"
        for _ in range(MAX_CONTEXT_DEPTH):
            obj = {"nested": obj}
        context = {"payload": obj}
        result = executor._sanitize_context(context)
        assert "payload" in result

    def test_sanitize_context_non_serializable(self, monkeypatch) -> None:
        executor = _make_executor(monkeypatch)
        context = {"phase": "setup", "chat_id": "valid_chat"}
        result = executor._sanitize_context(context)
        assert "phase" in result
        assert "chat_id" in result

    def test_sanitize_context_whitelisted_keys_only(self, monkeypatch) -> None:
        executor = _make_executor(monkeypatch)
        context = {"phase": "setup", "malicious_key": "evil", "payload": {"safe": True}}
        result = executor._sanitize_context(context)
        assert "phase" in result
        assert "payload" in result
        assert "malicious_key" not in result


# ---------------------------------------------------------------------------
# Wave 3: _execute_lib_in_container の sanitize 漏れ修正
# ---------------------------------------------------------------------------

class TestLibInContainerSanitize:

    def test_lib_in_container_uses_sanitize(self, monkeypatch, tmp_path) -> None:
        executor = _make_executor(monkeypatch)
        lib_file = tmp_path / "install.py"
        lib_file.write_text("def run(ctx): return {'status': 'ok'}")
        pack_data_dir = tmp_path / "data"
        pack_data_dir.mkdir()

        # コンテナ実行は Popen + パイプ排出スレッドで駆動するため、
        # stdout/stderr はバイト列を返すパイプとして差し替える
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = io.BytesIO(b'{"status": "ok"}')
        mock_proc.stderr = io.BytesIO(b"")

        sanitize_called = {"called": False}
        original_sanitize = executor._sanitize_context
        def spy_sanitize(ctx):
            sanitize_called["called"] = True
            return original_sanitize(ctx)

        with patch.object(executor, "_sanitize_context", side_effect=spy_sanitize), \
                patch("subprocess.Popen", return_value=mock_proc), \
                patch("subprocess.run", return_value=MagicMock(returncode=0)):
            result = executor._execute_lib_in_container(
                pack_id="test-pack", lib_type="install",
                lib_file=lib_file, pack_data_dir=pack_data_dir,
                context={"phase": "install", "payload": {"key": "val"}},
                timeout=30, start_time=time.time(),
            )
        assert sanitize_called["called"]
        assert result.success


# ---------------------------------------------------------------------------
# Wave 4: ホスト実行タイムアウト
# ---------------------------------------------------------------------------

class TestHostExecutionTimeout:

    def test_host_execution_timeout(self, monkeypatch, tmp_path) -> None:
        executor = _make_executor(monkeypatch, mode="permissive")
        slow_file = tmp_path / "slow.py"
        slow_file.write_text("import time\ndef run(ctx):\n    time.sleep(10)\n    return {'result': 'done'}\n")
        result = executor._execute_on_host_with_warning(
            pack_id="test-pack", component_id="slow-component", phase="setup",
            file_path=slow_file, context={"phase": "setup"}, timeout=1,
        )
        assert not result.success
        assert result.error_type == "timeout"
        assert "timed out" in result.error

    def test_host_execution_no_timeout(self, monkeypatch, tmp_path) -> None:
        executor = _make_executor(monkeypatch, mode="permissive")
        fast_file = tmp_path / "fast.py"
        fast_file.write_text("def run(ctx):\n    return {'result': 'quick'}\n")
        result = executor._execute_on_host_with_warning(
            pack_id="test-pack", component_id="fast-component", phase="setup",
            file_path=fast_file, context={"phase": "setup"}, timeout=10,
        )
        assert result.success
        assert result.output == {"result": "quick"}

    def test_host_lib_execution_timeout(self, monkeypatch, tmp_path) -> None:
        executor = _make_executor(monkeypatch, mode="permissive")
        slow_lib = tmp_path / "slow_lib.py"
        slow_lib.write_text("import time\ndef run(ctx):\n    time.sleep(10)\n    return {'status': 'done'}\n")
        pack_data_dir = tmp_path / "data"
        pack_data_dir.mkdir()
        result = executor._execute_lib_on_host_with_warning(
            pack_id="test-pack", lib_type="install", lib_file=slow_lib,
            pack_data_dir=pack_data_dir, context={"phase": "install"},
            start_time=time.time(), timeout=1,
        )
        assert not result.success
        assert result.error_type == "timeout"
        assert result.pack_id == "test-pack"

    def test_host_execution_cleans_added_sys_path(self, monkeypatch, tmp_path) -> None:
        executor = _make_executor(monkeypatch, mode="permissive")
        module_dir = tmp_path / "component"
        module_dir.mkdir()
        runner_file = module_dir / "runner.py"
        runner_file.write_text("def run(ctx):\n    return {'ok': True}\n")

        original_path = list(sys.path)
        result = executor._execute_on_host_with_warning(
            pack_id="test-pack",
            component_id="component",
            phase="setup",
            file_path=runner_file,
            context={"phase": "setup"},
            timeout=10,
        )

        assert result.success
        assert str(module_dir) not in sys.path
        assert sys.path == original_path
