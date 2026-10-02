"""
test_wave15e_di_container.py - Wave 15-E DI コンテナ基盤サービス登録テスト

テスト対象:
  - Wave 15 で追加した 3 サービス (health_checker, metrics_collector, profiler)
    が get_container() で取得できること
  - キャッシュが効くこと（2回目 get で同一インスタンス）
  - reset 後に新インスタンスが生成されること
  - 既存サービスが壊れていないこと
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import types
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# ダミーモジュール登録 — 動的インポートを安全にする
# ---------------------------------------------------------------------------
for _mod_name in [
    "backend_core",
    "backend_core.ecosystem",
    "backend_core.ecosystem.registry",
    "backend_core.ecosystem.active_ecosystem",
    "backend_core.ecosystem.mounts",
]:
    if _mod_name not in sys.modules:
        sys.modules[_mod_name] = types.ModuleType(_mod_name)

# pack_api_server ダミー
_dummy_pack_api = types.ModuleType("tobkiri_runtime.core_runtime.pack_api_server")


class _APIResponse:
    def __init__(self, success, data=None, error=None):
        self.success = success
        self.data = data
        self.error = error


_dummy_pack_api.APIResponse = _APIResponse
sys.modules.setdefault("tobkiri_runtime.core_runtime.pack_api_server", _dummy_pack_api)

# audit_logger ダミー
_dummy_audit = types.ModuleType("tobkiri_runtime.core_runtime.audit_logger")
_dummy_audit.get_audit_logger = MagicMock(return_value=MagicMock())
sys.modules.setdefault("tobkiri_runtime.core_runtime.audit_logger", _dummy_audit)

# ---------------------------------------------------------------------------
# テスト対象インポート
# ---------------------------------------------------------------------------
from tobkiri_runtime.core_runtime.di_container import (  # noqa: E402
    get_container,
    reset_container,
)
from tobkiri_runtime.core_runtime.health import HealthChecker  # noqa: E402
from tobkiri_runtime.core_runtime.metrics import MetricsCollector  # noqa: E402
from tobkiri_runtime.core_runtime.profiling import Profiler  # noqa: E402


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_ROOT = REPOSITORY_ROOT / "tobkiri_runtime"


@pytest.mark.parametrize(
    "first_prefix,second_prefix",
    [
        ("core_runtime", "tobkiri_runtime.core_runtime"),
        ("tobkiri_runtime.core_runtime", "core_runtime"),
    ],
)
def test_supported_module_aliases_share_di_class_identity(
    first_prefix: str,
    second_prefix: str,
) -> None:
    """Both supported import orders must resolve one module and class identity."""

    script = f"""
import importlib

first_prefix = {first_prefix!r}
second_prefix = {second_prefix!r}
services = {{
    "health": ("HealthChecker", "health_checker"),
    "metrics": ("MetricsCollector", "metrics_collector"),
    "profiling": ("Profiler", "profiler"),
}}
first_di = importlib.import_module(f"{{first_prefix}}.di_container")
second_di = importlib.import_module(f"{{second_prefix}}.di_container")
assert first_di is second_di
container = first_di.get_container()
for module_name, (class_name, service_name) in services.items():
    first = importlib.import_module(f"{{first_prefix}}.{{module_name}}")
    second = importlib.import_module(f"{{second_prefix}}.{{module_name}}")
    assert first is second
    assert getattr(first, class_name) is getattr(second, class_name)
    initial = container.get(service_name)
    assert type(initial) is getattr(first, class_name)
    container.reset(service_name)
    replacement = container.get(service_name)
    assert replacement is not initial
    assert type(replacement) is getattr(second, class_name)
"""
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join(
        (str(REPOSITORY_ROOT), str(RUNTIME_ROOT))
    )
    subprocess.run(
        [sys.executable, "-c", script],
        cwd=REPOSITORY_ROOT,
        env=environment,
        check=True,
    )


@pytest.mark.parametrize(
    "python_path,module_prefix",
    [
        (RUNTIME_ROOT, "core_runtime"),
        (REPOSITORY_ROOT, "tobkiri_runtime.core_runtime"),
    ],
)
def test_di_services_import_in_installed_and_repository_modes(
    python_path: Path,
    module_prefix: str,
) -> None:
    """Canonical installed and repository package layouts both initialize DI."""

    script = f"""
from {module_prefix}.di_container import get_container
from {module_prefix}.health import HealthChecker
from {module_prefix}.metrics import MetricsCollector
from {module_prefix}.profiling import Profiler

container = get_container()
assert type(container.get("health_checker")) is HealthChecker
assert type(container.get("metrics_collector")) is MetricsCollector
assert type(container.get("profiler")) is Profiler
"""
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(python_path)
    subprocess.run(
        [sys.executable, "-c", script],
        cwd=python_path,
        env=environment,
        check=True,
    )


# ======================================================================
# Fixture
# ======================================================================

@pytest.fixture(autouse=True)
def _reset_di():
    """各テストの前後で DI コンテナをリセット"""
    reset_container()
    yield
    reset_container()


# ======================================================================
# Wave 15 サービス登録テスト
# ======================================================================

class TestWave15Registration:
    """Wave 15 で追加された 3 サービスが DI コンテナに登録されていること"""

    def test_health_checker_registered(self):
        c = get_container()
        assert c.has("health_checker")

    def test_metrics_collector_registered(self):
        c = get_container()
        assert c.has("metrics_collector")

    def test_profiler_registered(self):
        c = get_container()
        assert c.has("profiler")


# ======================================================================
# Wave 15 サービス取得テスト
# ======================================================================

class TestWave15Get:
    """Wave 15 サービスが正しい型のインスタンスとして取得できること"""

    def test_get_health_checker(self):
        c = get_container()
        obj = c.get("health_checker")
        assert isinstance(obj, HealthChecker)

    def test_get_metrics_collector(self):
        c = get_container()
        obj = c.get("metrics_collector")
        assert isinstance(obj, MetricsCollector)

    def test_get_profiler(self):
        c = get_container()
        obj = c.get("profiler")
        assert isinstance(obj, Profiler)

    def test_get_or_none_health_checker(self):
        c = get_container()
        obj = c.get_or_none("health_checker")
        assert isinstance(obj, HealthChecker)

    def test_get_or_none_metrics_collector(self):
        c = get_container()
        obj = c.get_or_none("metrics_collector")
        assert isinstance(obj, MetricsCollector)

    def test_get_or_none_profiler(self):
        c = get_container()
        obj = c.get_or_none("profiler")
        assert isinstance(obj, Profiler)


# ======================================================================
# キャッシュテスト
# ======================================================================

class TestWave15Cache:
    """DI コンテナのキャッシュ動作確認: 2回目 get で同一インスタンス"""

    def test_health_checker_cached(self):
        c = get_container()
        obj1 = c.get("health_checker")
        obj2 = c.get("health_checker")
        assert obj1 is obj2

    def test_metrics_collector_cached(self):
        c = get_container()
        obj1 = c.get("metrics_collector")
        obj2 = c.get("metrics_collector")
        assert obj1 is obj2

    def test_profiler_cached(self):
        c = get_container()
        obj1 = c.get("profiler")
        obj2 = c.get("profiler")
        assert obj1 is obj2


# ======================================================================
# リセットテスト
# ======================================================================

class TestWave15Reset:
    """reset 後に新しいインスタンスが生成されること"""

    def test_reset_health_checker(self):
        c = get_container()
        obj1 = c.get("health_checker")
        c.reset("health_checker")
        obj2 = c.get("health_checker")
        assert obj1 is not obj2
        assert isinstance(obj2, HealthChecker)

    def test_reset_metrics_collector(self):
        c = get_container()
        obj1 = c.get("metrics_collector")
        c.reset("metrics_collector")
        obj2 = c.get("metrics_collector")
        assert obj1 is not obj2
        assert isinstance(obj2, MetricsCollector)

    def test_reset_profiler(self):
        c = get_container()
        obj1 = c.get("profiler")
        c.reset("profiler")
        obj2 = c.get("profiler")
        assert obj1 is not obj2
        assert isinstance(obj2, Profiler)

    def test_reset_container_recreates_all(self):
        c1 = get_container()
        hc1 = c1.get("health_checker")
        mc1 = c1.get("metrics_collector")
        pf1 = c1.get("profiler")
        reset_container()
        c2 = get_container()
        hc2 = c2.get("health_checker")
        mc2 = c2.get("metrics_collector")
        pf2 = c2.get("profiler")
        assert hc1 is not hc2
        assert mc1 is not mc2
        assert pf1 is not pf2


# ======================================================================
# 既存サービス互換性テスト
# ======================================================================

class TestExistingServicesNotBroken:
    """Wave 15 追加後も既存サービスが registered_names に含まれること"""

    def test_audit_logger_in_registered_names(self):
        c = get_container()
        names = c.registered_names()
        assert "audit_logger" in names

    def test_diagnostics_in_registered_names(self):
        c = get_container()
        names = c.registered_names()
        assert "diagnostics" in names

    def test_install_journal_in_registered_names(self):
        c = get_container()
        names = c.registered_names()
        assert "install_journal" in names

    def test_event_bus_in_registered_names(self):
        c = get_container()
        names = c.registered_names()
        assert "event_bus" in names

    def test_wave15_services_in_registered_names(self):
        c = get_container()
        names = c.registered_names()
        assert "health_checker" in names
        assert "metrics_collector" in names
        assert "profiler" in names

    def test_canonical_container_excludes_direct_executors(self):
        """Only Broker-installed execution may enter the canonical container."""
        c = get_container()
        names = set(c.registered_names())
        assert names.isdisjoint(
            {
                "container_orchestrator",
                "docker_capability_handler",
                "egress_proxy_manager",
                "flow_composer",
                "host_privilege_manager",
                "lib_executor",
                "modifier_applier",
                "modifier_loader",
                "python_file_executor",
                "secure_executor",
                "unit_executor",
            }
        )


# ======================================================================
# DI 取得後の基本機能テスト
# ======================================================================

class TestWave15Functionality:
    """DI 経由で取得したインスタンスが正常に動作すること"""

    def test_health_checker_has_aggregate_health(self):
        c = get_container()
        hc = c.get("health_checker")
        result = hc.aggregate_health()
        assert result["status"] == "UP"
        assert "probes" in result
        assert "timestamp" in result

    def test_metrics_collector_has_increment(self):
        c = get_container()
        mc = c.get("metrics_collector")
        mc.increment("test_counter")
        snap = mc.snapshot()
        assert "test_counter" in snap["counters"]

    def test_profiler_has_profile(self):
        c = get_container()
        pf = c.get("profiler")
        with pf.profile("test_section"):
            pass
        stats = pf.get_stats("test_section")
        assert stats is not None
        assert stats["count"] == 1
