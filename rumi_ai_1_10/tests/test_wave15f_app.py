"""
test_wave15f_app.py - Wave 15-F: app.py ヘルスチェック + ログ設定統合テスト

テスト対象:
  - configure_logging() が main() 起動時に呼ばれること
  - --health フラグでヘルスチェックが実行されること
  - 既存フラグ (--headless, --permissive, --validate) が壊れていないこと
"""

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch, ANY

# rumi_ai_1_10/ を sys.path に追加して import app を可能にする
_project_root = str(Path(__file__).resolve().parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)


def _expected_health_disk_path() -> str:
    if os.name == "nt":
        return os.environ.get("SystemDrive", "C:") + "\\"
    return "/"


class TestConfigureLoggingCalled(unittest.TestCase):
    """configure_logging() の呼び出し確認テスト。"""

    @patch("core_runtime.health.get_health_checker")
    @patch("core_runtime.health.probe_disk_space")
    @patch("core_runtime.health.probe_file_writable")
    @patch("core_runtime.logging_utils.configure_logging")
    def test_configure_logging_called_on_main(
        self, mock_configure, mock_pfw, mock_pds, mock_ghc
    ):
        """main() 起動時に configure_logging が1回呼ばれる。"""
        mock_checker = MagicMock()
        mock_checker.aggregate_health.return_value = {
            "status": "UP", "timestamp": "T", "probes": {}
        }
        mock_ghc.return_value = mock_checker

        with patch("sys.argv", ["app.py", "--health"]):
            from app import main
            buf = io.StringIO()
            with redirect_stdout(buf), self.assertRaises(SystemExit):
                main()
        mock_configure.assert_called_once()

    @patch("core_runtime.health.get_health_checker")
    @patch("core_runtime.health.probe_disk_space")
    @patch("core_runtime.health.probe_file_writable")
    @patch("core_runtime.logging_utils.configure_logging")
    def test_configure_logging_default_params(
        self, mock_configure, mock_pfw, mock_pds, mock_ghc
    ):
        """環境変数未設定時はデフォルト (INFO, json) で呼ばれる。"""
        mock_checker = MagicMock()
        mock_checker.aggregate_health.return_value = {
            "status": "UP", "timestamp": "T", "probes": {}
        }
        mock_ghc.return_value = mock_checker

        env = {k: v for k, v in os.environ.items()
               if k not in ("RUMI_LOG_LEVEL", "RUMI_LOG_FORMAT")}
        with patch("sys.argv", ["app.py", "--health"]), \
             patch.dict("os.environ", env, clear=True):
            from app import main
            buf = io.StringIO()
            with redirect_stdout(buf), self.assertRaises(SystemExit):
                main()
        mock_configure.assert_called_once_with(level="INFO", fmt="json")

    @patch("core_runtime.health.get_health_checker")
    @patch("core_runtime.health.probe_disk_space")
    @patch("core_runtime.health.probe_file_writable")
    @patch("core_runtime.logging_utils.configure_logging")
    def test_configure_logging_env_override(
        self, mock_configure, mock_pfw, mock_pds, mock_ghc
    ):
        """環境変数でレベルとフォーマットを上書きできる。"""
        mock_checker = MagicMock()
        mock_checker.aggregate_health.return_value = {
            "status": "UP", "timestamp": "T", "probes": {}
        }
        mock_ghc.return_value = mock_checker

        with patch("sys.argv", ["app.py", "--health"]), \
             patch.dict("os.environ", {
                 "RUMI_LOG_LEVEL": "DEBUG",
                 "RUMI_LOG_FORMAT": "text",
             }):
            from app import main
            buf = io.StringIO()
            with redirect_stdout(buf), self.assertRaises(SystemExit):
                main()
        mock_configure.assert_called_once_with(level="DEBUG", fmt="text")

    @patch("core_runtime.health.get_health_checker")
    @patch("core_runtime.health.probe_disk_space")
    @patch("core_runtime.health.probe_file_writable")
    @patch("core_runtime.logging_utils.configure_logging")
    def test_configure_logging_called_once_not_twice(
        self, mock_configure, mock_pfw, mock_pds, mock_ghc
    ):
        """main() 内で configure_logging は1回だけ呼ばれる。"""
        mock_checker = MagicMock()
        mock_checker.aggregate_health.return_value = {
            "status": "UP", "timestamp": "T", "probes": {}
        }
        mock_ghc.return_value = mock_checker

        with patch("sys.argv", ["app.py", "--health"]):
            from app import main
            buf = io.StringIO()
            with redirect_stdout(buf), self.assertRaises(SystemExit):
                main()
        self.assertEqual(mock_configure.call_count, 1)


class TestHealthFlag(unittest.TestCase):
    """--health フラグのテスト。"""

    @patch("core_runtime.health.probe_file_writable")
    @patch("core_runtime.health.probe_disk_space")
    @patch("core_runtime.health.get_health_checker")
    @patch("core_runtime.logging_utils.configure_logging")
    def test_health_flag_exits_zero_on_up(
        self, mock_configure, mock_ghc, mock_pds, mock_pfw
    ):
        """status=UP で exit code 0。"""
        mock_checker = MagicMock()
        mock_checker.aggregate_health.return_value = {
            "status": "UP", "timestamp": "T", "probes": {}
        }
        mock_ghc.return_value = mock_checker

        with patch("sys.argv", ["app.py", "--health"]):
            from app import main
            buf = io.StringIO()
            with redirect_stdout(buf), self.assertRaises(SystemExit) as ctx:
                main()
        self.assertEqual(ctx.exception.code, 0)

    @patch("core_runtime.health.probe_file_writable")
    @patch("core_runtime.health.probe_disk_space")
    @patch("core_runtime.health.get_health_checker")
    @patch("core_runtime.logging_utils.configure_logging")
    def test_health_flag_exits_one_on_down(
        self, mock_configure, mock_ghc, mock_pds, mock_pfw
    ):
        """status=DOWN で exit code 1。"""
        mock_checker = MagicMock()
        mock_checker.aggregate_health.return_value = {
            "status": "DOWN", "timestamp": "T", "probes": {}
        }
        mock_ghc.return_value = mock_checker

        with patch("sys.argv", ["app.py", "--health"]):
            from app import main
            buf = io.StringIO()
            with redirect_stdout(buf), self.assertRaises(SystemExit) as ctx:
                main()
        self.assertEqual(ctx.exception.code, 1)

    @patch("core_runtime.health.probe_file_writable")
    @patch("core_runtime.health.probe_disk_space")
    @patch("core_runtime.health.get_health_checker")
    @patch("core_runtime.logging_utils.configure_logging")
    def test_health_flag_outputs_valid_json(
        self, mock_configure, mock_ghc, mock_pds, mock_pfw
    ):
        """--health の stdout 出力が有効な JSON であること。"""
        expected = {
            "status": "UP", "timestamp": "2025-01-01T00:00:00Z",
            "probes": {"disk": {"status": "UP", "message": "ok", "duration_ms": 1.0}}
        }
        mock_checker = MagicMock()
        mock_checker.aggregate_health.return_value = expected
        mock_ghc.return_value = mock_checker

        with patch("sys.argv", ["app.py", "--health"]):
            from app import main
            buf = io.StringIO()
            with redirect_stdout(buf), self.assertRaises(SystemExit):
                main()
        output = json.loads(buf.getvalue())
        self.assertEqual(output["status"], "UP")
        self.assertIn("probes", output)

    @patch("core_runtime.health.probe_file_writable")
    @patch("core_runtime.health.probe_disk_space")
    @patch("core_runtime.health.get_health_checker")
    @patch("core_runtime.logging_utils.configure_logging")
    def test_health_registers_disk_probe(
        self, mock_configure, mock_ghc, mock_pds, mock_pfw
    ):
        """disk プローブが register_probe で登録される。"""
        mock_checker = MagicMock()
        mock_checker.aggregate_health.return_value = {
            "status": "UP", "timestamp": "T", "probes": {}
        }
        mock_ghc.return_value = mock_checker

        with patch("sys.argv", ["app.py", "--health"]):
            from app import main
            buf = io.StringIO()
            with redirect_stdout(buf), self.assertRaises(SystemExit):
                main()

        # register_probe が "disk" で呼ばれたか確認
        calls = mock_checker.register_probe.call_args_list
        probe_names = [c[0][0] for c in calls]
        self.assertIn("disk", probe_names)
        disk_probe = next(c[0][1] for c in calls if c[0][0] == "disk")
        disk_probe()
        mock_pds.assert_called_with(_expected_health_disk_path())

    @patch("core_runtime.health.probe_file_writable")
    @patch("core_runtime.health.probe_disk_space")
    @patch("core_runtime.health.get_health_checker")
    @patch("core_runtime.logging_utils.configure_logging")
    def test_health_registers_writable_tmp_probe(
        self, mock_configure, mock_ghc, mock_pds, mock_pfw
    ):
        """writable_tmp プローブが register_probe で登録される。"""
        mock_checker = MagicMock()
        mock_checker.aggregate_health.return_value = {
            "status": "UP", "timestamp": "T", "probes": {}
        }
        mock_ghc.return_value = mock_checker

        with patch("sys.argv", ["app.py", "--health"]):
            from app import main
            buf = io.StringIO()
            with redirect_stdout(buf), self.assertRaises(SystemExit):
                main()

        calls = mock_checker.register_probe.call_args_list
        probe_names = [c[0][0] for c in calls]
        self.assertIn("writable_tmp", probe_names)
        tmp_probe = next(c[0][1] for c in calls if c[0][0] == "writable_tmp")
        tmp_probe()
        mock_pfw.assert_called_with(tempfile.gettempdir())

    @patch("core_runtime.health.probe_file_writable")
    @patch("core_runtime.health.probe_disk_space")
    @patch("core_runtime.health.get_health_checker")
    @patch("core_runtime.logging_utils.configure_logging")
    def test_health_uses_windows_disk_path_on_nt(
        self, mock_configure, mock_ghc, mock_pds, mock_pfw
    ):
        """Windows では SystemDrive を disk プローブに使う。"""
        mock_checker = MagicMock()
        mock_checker.aggregate_health.return_value = {
            "status": "UP", "timestamp": "T", "probes": {}
        }
        mock_ghc.return_value = mock_checker

        with patch("sys.argv", ["app.py", "--health"]), \
             patch("os.name", "nt"), \
             patch.dict("os.environ", {"SystemDrive": "D:"}, clear=False):
            from app import main
            buf = io.StringIO()
            with redirect_stdout(buf), self.assertRaises(SystemExit):
                main()

        disk_probe = next(
            c[0][1] for c in mock_checker.register_probe.call_args_list if c[0][0] == "disk"
        )
        disk_probe()
        mock_pds.assert_called_with("D:\\")


class TestExistingFlagsNotBroken(unittest.TestCase):
    """既存フラグの後方互換性テスト。"""

    @patch("core_runtime.logging_utils.configure_logging")
    @patch("app._run_validation")
    def test_validate_flag_still_works(self, mock_validate, mock_configure):
        """--validate フラグが引き続き _run_validation を呼ぶ。"""
        with patch("sys.argv", ["app.py", "--validate"]):
            from app import main
            main()
        mock_validate.assert_called_once()

    @patch("core_runtime.logging_utils.configure_logging")
    def test_headless_flag_still_works(self, mock_configure):
        """--headless フラグが Kernel 起動後に早期リターンする。"""
        mock_kernel_instance = MagicMock()

        # This verifies the default headless path.  Isolate it from a prior
        # test's process-wide mode so an unrelated permissive request cannot
        # turn this compatibility test into a permissive-mode guard test.
        with patch("sys.argv", ["app.py", "--headless"]), \
             patch.dict(os.environ, {"RUMI_SECURITY_MODE": "strict"}, clear=False), \
             patch("core_runtime.Kernel", return_value=mock_kernel_instance, create=True), \
             patch("core_runtime.lang.L", side_effect=lambda k, **kw: k), \
             patch("core_runtime.lang.load_system_lang"):
            from app import main
            main()  # headless なので return で終了
        mock_kernel_instance.run_startup.assert_called_once()

    @patch("core_runtime.health.probe_file_writable")
    @patch("core_runtime.health.probe_disk_space")
    @patch("core_runtime.health.get_health_checker")
    @patch("core_runtime.logging_utils.configure_logging")
    def test_health_evaluated_before_validate(
        self, mock_configure, mock_ghc, mock_pds, mock_pfw
    ):
        """--health と --validate 両方指定時、--health が優先される。"""
        mock_checker = MagicMock()
        mock_checker.aggregate_health.return_value = {
            "status": "UP", "timestamp": "T", "probes": {}
        }
        mock_ghc.return_value = mock_checker

        with patch("sys.argv", ["app.py", "--health", "--validate"]), \
             patch("app._run_validation") as mock_validate:
            from app import main
            buf = io.StringIO()
            with redirect_stdout(buf), self.assertRaises(SystemExit) as ctx:
                main()
        # health で exit するので validate は呼ばれない
        self.assertEqual(ctx.exception.code, 0)
        mock_validate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
