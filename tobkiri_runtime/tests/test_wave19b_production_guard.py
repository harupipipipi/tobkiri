"""
W19-B  VULN-C01 — production 環境での --permissive 起動拒否ガード

テスト対象: app.main() 内の production ガード
依存モジュール (core_runtime 等) は全てモック化して実行する。
"""
import importlib
import os
import sys
import types
from unittest.mock import MagicMock, patch

import pytest

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
        elif name == "core_runtime.pack_validator":
            mod.validate_host_execution = MagicMock()
            mod.validate_host_execution_single = MagicMock(return_value=(True, ""))
        elif name == "core_runtime.kernel_facade":
            mod.KernelFacade = MagicMock()
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


def _run_main(*argv, env=None):
    """app.main() をカスタム argv / 環境変数で呼び出すヘルパー。"""
    old_argv = sys.argv[:]
    old_env = {}
    try:
        sys.argv = ["app.py"] + list(argv)
        if env:
            for k, v in env.items():
                old_env[k] = os.environ.get(k)
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        import app
        importlib.reload(app)
        app.main()
    finally:
        sys.argv = old_argv
        for k, v in old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ===================================================================
# テストケース (5 件)
# ===================================================================

class TestProductionGuard:

    def test_production_permissive_rejected(self):
        """Production cannot opt into the retired permissive CLI."""
        with pytest.raises(SystemExit) as exc:
            _run_main("--permissive", env={"RUMI_ENVIRONMENT": "production"})
        assert exc.value.code == 2

    def test_development_permissive_allowed(self, tmp_path):
        """Development cannot restore the retired permissive CLI."""
        (tmp_path / "permissive.lock").touch()
        with pytest.raises(SystemExit) as exc:
            _run_main(
                "--permissive",
                "--headless",
                env={
                    "RUMI_ENVIRONMENT": "development",
                    "RUMI_USER_DATA": str(tmp_path),
                },
            )
        assert exc.value.code == 2

    def test_unset_env_permissive_allowed(self):
        """An unset environment cannot restore the retired permissive CLI."""
        with pytest.raises(SystemExit) as exc:
            _run_main("--permissive", "--headless",
                      env={"RUMI_ENVIRONMENT": None, "RUMI_ALLOW_PERMISSIVE": None})
        assert exc.value.code == 2

    def test_production_uppercase_permissive_rejected(self):
        """Case changes cannot restore the retired permissive CLI."""
        with pytest.raises(SystemExit) as exc:
            _run_main("--permissive", env={"RUMI_ENVIRONMENT": "PRODUCTION"})
        assert exc.value.code == 2

    def test_no_permissive_normal_startup(self):
        """The current headless v4 entrypoint starts without permissive state."""
        import app

        kernel = MagicMock()
        with patch.object(app, "Kernel", return_value=kernel):
            assert app.main(["--headless"]) == 0
        kernel.run_startup.assert_called_once()
        kernel.shutdown.assert_called_once()
