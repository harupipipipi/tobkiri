"""Pack v4 production-root retirement tests for the old permissive surface."""
from __future__ import annotations

import importlib.util
import inspect
import sys
import types
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# app.py を importlib で安全にロード（副作用回避）
# ---------------------------------------------------------------------------
_APP_PATH = Path(__file__).resolve().parent.parent / "app.py"


def _load_app():
    spec = importlib.util.spec_from_file_location("_app_under_test", str(_APP_PATH))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_app = _load_app()


def _assert_retired_production_root() -> None:
    """The canonical root has no legacy guard, env switch, or flag."""
    source = _APP_PATH.read_text(encoding="utf-8")
    assert not hasattr(_app, "_check_permissive_production_guard")
    assert "RUMI_ALLOW_PERMISSIVE" not in source
    assert "permissive.lock" not in source
    with pytest.raises(SystemExit):
        _app._parser().parse_args(["--permissive"])


# =========================================================================
# Group A: _check_permissive_production_guard() 直接テスト
# =========================================================================
class TestCheckPermissiveProductionGuard:
    """Every historical guard input is rejected at the v4 root boundary."""

    def test_production_exact_exits(self, monkeypatch):
        """The production label cannot resurrect the removed guard."""
        monkeypatch.setenv("RUMI_ENVIRONMENT", "production")
        _assert_retired_production_root()

    def test_production_mixed_case_exits(self, monkeypatch):
        """Mixed-case production labels have no special API surface."""
        monkeypatch.setenv("RUMI_ENVIRONMENT", "Production")
        _assert_retired_production_root()

    def test_production_upper_exits(self, monkeypatch):
        """Upper-case production labels have no special API surface."""
        monkeypatch.setenv("RUMI_ENVIRONMENT", "PRODUCTION")
        _assert_retired_production_root()

    def test_production_rejects_even_an_explicit_allow_flag(self, monkeypatch, tmp_path):
        """Production cannot add a flag even with stale opt-in artifacts."""
        (tmp_path / "permissive.lock").touch()
        monkeypatch.setenv("RUMI_ENVIRONMENT", "production")
        monkeypatch.setenv("RUMI_ALLOW_PERMISSIVE", "true")
        monkeypatch.setenv("RUMI_USER_DATA", str(tmp_path))
        _assert_retired_production_root()

    def test_development_does_not_exit(self, monkeypatch, tmp_path):
        """Development labels cannot bypass the production-root retirement."""
        monkeypatch.setenv("RUMI_ENVIRONMENT", "development")
        (tmp_path / "permissive.lock").touch()
        monkeypatch.setenv("RUMI_USER_DATA", str(tmp_path))
        _assert_retired_production_root()

    def test_unset_does_not_exit(self, monkeypatch):
        """Unset environment state still exposes no permissive switch."""
        monkeypatch.delenv("RUMI_ENVIRONMENT", raising=False)
        _assert_retired_production_root()

    def test_guard_calls_sys_exit_1(self, monkeypatch):
        """The old callable is physically absent rather than shimmed."""
        monkeypatch.setenv("RUMI_ENVIRONMENT", "production")
        _assert_retired_production_root()

    def test_guard_prints_fatal_to_stderr(self, monkeypatch, capsys):
        """The parser reports the retired flag, not a permissive guard error."""
        monkeypatch.setenv("RUMI_ENVIRONMENT", "production")
        _assert_retired_production_root()
        captured = capsys.readouterr()
        assert "unrecognized arguments" in captured.err
        assert "--permissive" in captured.err


# =========================================================================
# Group B: main() フローおよびソースコード検査
# =========================================================================
class TestMainPermissiveFlow:
    """main() 経由の統合テストおよびコード検査"""

    @pytest.fixture(autouse=True)
    def _mock_logging(self, monkeypatch):
        """main() を呼ぶ際に必要な core_runtime.logging_utils のスタブ"""
        stub = types.ModuleType("core_runtime.logging_utils")
        stub.configure_logging = lambda **kw: None
        monkeypatch.setitem(sys.modules, "core_runtime.logging_utils", stub)
        yield

    def test_no_permissive_production_sets_strict(self, monkeypatch):
        """The production root has no mode-setting compatibility path."""
        monkeypatch.setenv("RUMI_ENVIRONMENT", "production")
        _assert_retired_production_root()
        assert _app._parser().parse_args(["--headless"]).headless is True

    def test_no_duplicate_env_check_in_main(self):
        """main() contains no legacy environment authority."""
        source = inspect.getsource(_app.main)
        assert "RUMI_ENVIRONMENT" not in source
        assert "RUMI_ALLOW_PERMISSIVE" not in source

    def test_permissive_sets_security_mode(self, monkeypatch, tmp_path):
        """The retired flag cannot set a security mode."""
        monkeypatch.setenv("RUMI_ENVIRONMENT", "development")
        (tmp_path / "permissive.lock").touch()
        monkeypatch.setenv("RUMI_USER_DATA", str(tmp_path))
        _assert_retired_production_root()

    def test_environment_permissive_requires_the_same_guard(self, monkeypatch):
        """A stale environment value cannot create a hidden execution mode."""
        monkeypatch.setenv("RUMI_SECURITY_MODE", "permissive")
        monkeypatch.delenv("RUMI_ALLOW_PERMISSIVE", raising=False)
        monkeypatch.delenv("RUMI_ENVIRONMENT", raising=False)
        _assert_retired_production_root()

    def test_environment_permissive_opt_in_reaches_guard_boundary(self, monkeypatch, tmp_path):
        """A development opt-in remains data, not a production-root command."""
        (tmp_path / "permissive.lock").touch()
        monkeypatch.setenv("RUMI_ENVIRONMENT", "development")
        monkeypatch.setenv("TOBKIRI_USER_DATA", str(tmp_path))
        monkeypatch.setenv("RUMI_USER_DATA", str(tmp_path))
        monkeypatch.setenv("RUMI_SECURITY_MODE", "permissive")
        _assert_retired_production_root()
