"""
test_app1_permissive_guard.py - APP-1: permissive ガード強化 + _w19d_* リネーム検証

テスト対象:
  - _check_permissive_production_guard のホワイトリスト方式ガード
  - _w19d_* プレフィックスの除去確認

依存モジュール (core_runtime 等) は全てモック化して実行する。
"""
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.contract

# ---------------------------------------------------------------------------
# core_runtime 等の外部依存を一括モック
# ---------------------------------------------------------------------------
_MOCK_MODULES = [
    "core_runtime",
    "core_runtime.logging_utils",
    "core_runtime.lang",
    "core_runtime.kernel_facade",
    "core_runtime.health",
    "core_runtime.pack_validator",
    "core_runtime.paths",
    "backend_core",
    "backend_core.ecosystem",
    "backend_core.ecosystem.compat",
    "backend_core.ecosystem.active_ecosystem",
]


@pytest.fixture(autouse=True)
def _mock_deps():
    """各テスト前にモックモジュールを仕込み、テスト後に除去する。"""
    saved = {n: sys.modules.get(n) for n in _MOCK_MODULES}
    saved["app"] = sys.modules.get("app")

    for name in _MOCK_MODULES:
        mod = types.ModuleType(name)
        if name == "core_runtime.logging_utils":
            mod.configure_logging = MagicMock()
        elif name == "core_runtime":
            ki = MagicMock()
            ki.interface_registry.get.return_value = None
            mod.Kernel = MagicMock(return_value=ki)
        elif name == "core_runtime.lang":
            mod.L = lambda key, **kw: key
            mod.load_system_lang = MagicMock()
        elif name == "core_runtime.kernel_facade":
            mod.KernelFacade = MagicMock()
        elif name == "core_runtime.pack_validator":
            mod.validate_host_execution = MagicMock()
            mod.validate_host_execution_single = MagicMock(
                return_value=(True, ""),
            )
        elif name == "core_runtime.paths":
            mod.discover_pack_locations = MagicMock(return_value=[])
        elif name == "backend_core.ecosystem.compat":
            mod.mark_ecosystem_initialized = MagicMock()
        elif name == "backend_core.ecosystem.active_ecosystem":
            mod.get_active_ecosystem_manager = MagicMock()
        sys.modules[name] = mod

    sys.modules.pop("app", None)

    yield

    sys.modules.pop("app", None)
    for name in _MOCK_MODULES:
        if saved[name] is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = saved[name]


def _assert_retired_production_root() -> None:
    """The canonical root exposes no legacy permissive guard or flag."""
    import app

    source = Path(app.__file__).read_text(encoding="utf-8")
    assert not hasattr(app, "_check_permissive_production_guard")
    assert "RUMI_ALLOW_PERMISSIVE" not in source
    assert "permissive.lock" not in source
    with pytest.raises(SystemExit):
        app._parser().parse_args(["--permissive"])


# ===================================================================
# Test cases: retired permissive surface
# ===================================================================

class TestPermissiveGuardStrengthened:
    """Old opt-in combinations cannot restore the retired flag."""

    def test_permissive_blocked_by_default(self):
        """No environment values add a permissive parser option."""
        _assert_retired_production_root()

    def test_permissive_blocked_in_production(self):
        """Production labels cannot alter the finite parser surface."""
        _assert_retired_production_root()

    def test_permissive_allowed_with_explicit_flag(self, tmp_path):
        """An old explicit allow flag is not accepted by the root."""
        (tmp_path / "permissive.lock").touch()
        _assert_retired_production_root()
        import app

        assert app._parser().parse_args(["--headless"]).headless is True

    def test_permissive_allowed_in_dev_environment(self, tmp_path):
        """Development labels cannot restore a removed opt-in."""
        (tmp_path / "permissive.lock").touch()
        _assert_retired_production_root()

    def test_permissive_allowed_in_dev_short(self, tmp_path):
        """The short development label is not a production-root authority."""
        (tmp_path / "permissive.lock").touch()
        _assert_retired_production_root()


# ===================================================================
# テストケース: _w19d_* 変数リネーム検証
# ===================================================================

class TestW19dVariablesRenamed:
    """_w19d_* プレフィックスが除去されていることの検証。"""

    def test_w19d_variables_renamed(self):
        """app.py 内に _w19d_ が存在しないこと。"""
        app_path = Path(__file__).resolve().parent.parent / "app.py"
        content = app_path.read_text(encoding="utf-8")
        assert "_w19d_" not in content, (
            "app.py still contains _w19d_ prefix"
        )
