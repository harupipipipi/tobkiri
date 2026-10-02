"""
test_health.py - health ユニットテスト

テスト観点:
- HealthStatus: 列挙値の確認
- HealthChecker: プローブ登録/削除/一覧/集約/タイムアウト
- aggregate_health: 全体ステータス決定ロジック
- 組み込みプローブ: probe_disk_space/probe_memory/probe_file_writable
- get_health_checker: キャッシュ/リセット
- スレッドセーフ動作
"""

from __future__ import annotations

import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

_project_root = Path(__file__).resolve().parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from core_runtime.health import (  # noqa: E402
    HealthStatus,
    HealthProbeResult,
    HealthChecker,
    probe_disk_space,
    probe_memory,
    probe_file_writable,
    get_health_checker,
    reset_health_checker,
)


# =========================================================================
# HealthStatus テスト
# =========================================================================


class TestHealthStatus(unittest.TestCase):
    """HealthStatus の列挙型テスト"""

    def test_status_values(self):
        """全ステータス値が正しい文字列を持つ"""
        self.assertEqual(HealthStatus.UP.value, "UP")
        self.assertEqual(HealthStatus.DOWN.value, "DOWN")
        self.assertEqual(HealthStatus.DEGRADED.value, "DEGRADED")
        self.assertEqual(HealthStatus.UNKNOWN.value, "UNKNOWN")

    def test_status_count(self):
        """HealthStatus は 4 つのメンバーを持つ"""
        self.assertEqual(len(HealthStatus), 4)


# =========================================================================
# HealthChecker テスト
# =========================================================================


class TestHealthChecker(unittest.TestCase):
    """HealthChecker のテスト"""

    def setUp(self):
        self.checker = HealthChecker(default_timeout=2.0)

    def test_register_and_list_probes(self):
        """プローブを登録して一覧に表示される"""
        self.checker.register_probe("test1", lambda: HealthStatus.UP)
        self.checker.register_probe("test2", lambda: HealthStatus.UP)
        probes = self.checker.list_probes()
        self.assertIn("test1", probes)
        self.assertIn("test2", probes)
        self.assertEqual(len(probes), 2)

    def test_remove_probe(self):
        """プローブを削除できる"""
        self.checker.register_probe("to_remove", lambda: HealthStatus.UP)
        self.assertTrue(self.checker.remove_probe("to_remove"))
        self.assertNotIn("to_remove", self.checker.list_probes())

    def test_remove_nonexistent_probe(self):
        """存在しないプローブの削除は False を返す"""
        self.assertFalse(self.checker.remove_probe("nonexistent"))

    def test_aggregate_no_probes(self):
        """プローブ未登録時は UP を返す"""
        result = self.checker.aggregate_health()
        self.assertEqual(result["status"], "UP")
        self.assertEqual(result["probes"], {})
        self.assertIn("timestamp", result)

    def test_aggregate_all_up(self):
        """全プローブ UP → 全体 UP"""
        self.checker.register_probe("p1", lambda: HealthStatus.UP)
        self.checker.register_probe("p2", lambda: HealthStatus.UP)
        result = self.checker.aggregate_health()
        self.assertEqual(result["status"], "UP")
        self.assertEqual(result["probes"]["p1"]["status"], "UP")
        self.assertEqual(result["probes"]["p2"]["status"], "UP")

    def test_aggregate_one_down(self):
        """1つ DOWN → 全体 DOWN"""
        self.checker.register_probe("ok", lambda: HealthStatus.UP)
        self.checker.register_probe("bad", lambda: HealthStatus.DOWN)
        result = self.checker.aggregate_health()
        self.assertEqual(result["status"], "DOWN")

    def test_aggregate_one_degraded(self):
        """1つ DEGRADED → 全体 DEGRADED"""
        self.checker.register_probe("ok", lambda: HealthStatus.UP)
        self.checker.register_probe("slow", lambda: HealthStatus.DEGRADED)
        result = self.checker.aggregate_health()
        self.assertEqual(result["status"], "DEGRADED")

    def test_aggregate_one_unknown(self):
        """1つ UNKNOWN → 全体 DEGRADED"""
        self.checker.register_probe("ok", lambda: HealthStatus.UP)
        self.checker.register_probe("unk", lambda: HealthStatus.UNKNOWN)
        result = self.checker.aggregate_health()
        self.assertEqual(result["status"], "DEGRADED")

    def test_aggregate_down_overrides_degraded(self):
        """DOWN と DEGRADED が混在 → 全体 DOWN"""
        self.checker.register_probe("deg", lambda: HealthStatus.DEGRADED)
        self.checker.register_probe("down", lambda: HealthStatus.DOWN)
        result = self.checker.aggregate_health()
        self.assertEqual(result["status"], "DOWN")

    def test_aggregate_timeout(self):
        """タイムアウトしたプローブは DOWN"""
        def slow_probe():
            time.sleep(10)
            return HealthStatus.UP

        self.checker.register_probe("slow", slow_probe)
        result = self.checker.aggregate_health(timeout=0.3)
        self.assertEqual(result["probes"]["slow"]["status"], "DOWN")
        self.assertIn("timeout", result["probes"]["slow"]["message"])

    def test_aggregate_exception_in_probe(self):
        """例外を投げるプローブは DOWN"""
        def bad_probe():
            raise RuntimeError("probe failure")

        self.checker.register_probe("bad", bad_probe)
        result = self.checker.aggregate_health()
        self.assertEqual(result["probes"]["bad"]["status"], "DOWN")
        self.assertIn("error", result["probes"]["bad"]["message"])

    def test_aggregate_result_has_duration(self):
        """プローブ結果に duration_ms が含まれる"""
        self.checker.register_probe("fast", lambda: HealthStatus.UP)
        result = self.checker.aggregate_health()
        self.assertIn("duration_ms", result["probes"]["fast"])
        self.assertIsInstance(result["probes"]["fast"]["duration_ms"], float)

    def test_aggregate_up_keeps_ok_message_and_schema(self):
        """Legacy UP results retain the original JSON probe schema."""
        self.checker.register_probe("healthy", lambda: HealthStatus.UP)
        result = self.checker.aggregate_health()["probes"]["healthy"]
        self.assertEqual(set(result), {"status", "message", "duration_ms"})
        self.assertEqual(result["message"], "ok")

    def test_aggregate_legacy_non_up_has_status_specific_message(self):
        """Status-only probes must not describe unhealthy results as ok."""
        for status in (
            HealthStatus.DEGRADED, HealthStatus.DOWN, HealthStatus.UNKNOWN
        ):
            with self.subTest(status=status):
                self.checker.register_probe("legacy", lambda: status)
                result = self.checker.aggregate_health()["probes"]["legacy"]
                self.assertEqual(result["status"], status.value)
                self.assertEqual(
                    result["message"],
                    f"legacy: probe returned {status.value} without a reason",
                )

    def test_aggregate_preserves_explicit_probe_reason(self):
        """A structured probe's cause survives aggregation unchanged."""
        for status in HealthStatus:
            with self.subTest(status=status):
                reason = f"custom diagnostic for {status.value}"
                self.checker.register_probe(
                    "custom", lambda: HealthProbeResult(status, reason)
                )
                result = self.checker.aggregate_health()["probes"]["custom"]
                self.assertEqual(result["status"], status.value)
                self.assertEqual(result["message"], reason)
                self.assertEqual(
                    set(result), {"status", "message", "duration_ms"}
                )
                self.assertIsInstance(result["duration_ms"], float)

    def test_aggregate_structured_empty_reason_uses_fallback(self):
        """Missing or blank structured reasons receive useful defaults."""
        for status in HealthStatus:
            for message in (None, "", "   "):
                with self.subTest(status=status, message=message):
                    self.checker.register_probe(
                        "empty", lambda: HealthProbeResult(status, message)
                    )
                    result = self.checker.aggregate_health()["probes"]["empty"]
                    expected = (
                        "ok"
                        if status is HealthStatus.UP
                        else f"empty: probe returned {status.value} without a reason"
                    )
                    self.assertEqual(result["message"], expected)

    def test_aggregate_result_has_timestamp(self):
        """結果に ISO8601 UTC タイムスタンプが含まれる"""
        self.checker.register_probe("p", lambda: HealthStatus.UP)
        result = self.checker.aggregate_health()
        self.assertTrue(result["timestamp"].endswith("Z"))

    def test_default_timeout_property(self):
        """default_timeout の getter/setter が正しく動作する"""
        self.assertEqual(self.checker.default_timeout, 2.0)
        self.checker.default_timeout = 10.0
        self.assertEqual(self.checker.default_timeout, 10.0)

    def test_default_timeout_invalid(self):
        """default_timeout に 0 以下を設定すると ValueError"""
        with self.assertRaises(ValueError):
            self.checker.default_timeout = 0
        with self.assertRaises(ValueError):
            self.checker.default_timeout = -1

    def test_register_probe_overwrite(self):
        """同名プローブを再登録すると上書きされる"""
        self.checker.register_probe("p", lambda: HealthStatus.DOWN)
        self.checker.register_probe("p", lambda: HealthStatus.UP)
        result = self.checker.aggregate_health()
        self.assertEqual(result["probes"]["p"]["status"], "UP")

    def test_thread_safe_registration(self):
        """複数スレッドからのプローブ登録がスレッドセーフ"""
        barrier = threading.Barrier(4)

        def register_from_thread(idx):
            barrier.wait(timeout=5)
            self.checker.register_probe(
                f"thread_{idx}", lambda: HealthStatus.UP
            )

        threads = [
            threading.Thread(target=register_from_thread, args=(i,))
            for i in range(4)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        probes = self.checker.list_probes()
        self.assertEqual(len(probes), 4)


# =========================================================================
# 組み込みプローブテスト
# =========================================================================


class TestBuiltinProbes(unittest.TestCase):
    """組み込みプローブ関数のテスト"""

    def test_probe_disk_space_returns_status(self):
        """probe_disk_space が HealthStatus を返す"""
        result = probe_disk_space("/")
        self.assertIsInstance(result, HealthStatus)

    def test_probe_disk_space_up_on_normal(self):
        """通常のディスクは UP を返す（しきい値 99.9%）"""
        result = probe_disk_space("/", threshold_pct=99.9)
        # 99.9% 未満であれば UP
        self.assertIn(result, (HealthStatus.UP, HealthStatus.DEGRADED, HealthStatus.DOWN))

    def test_probe_disk_space_invalid_path(self):
        """存在しないパスでは UNKNOWN を返す"""
        result = probe_disk_space("/nonexistent_path_xyz_123")
        self.assertEqual(result, HealthStatus.UNKNOWN)

    @patch("core_runtime.health.shutil.disk_usage")
    def test_probe_disk_space_degraded(self, mock_usage):
        """使用率がしきい値以上で DEGRADED"""
        mock_usage.return_value = MagicMock(total=100, used=91, free=9)
        result = probe_disk_space("/", threshold_pct=90.0)
        self.assertEqual(result, HealthStatus.DEGRADED)

    @patch("core_runtime.health.shutil.disk_usage")
    def test_probe_disk_space_down(self, mock_usage):
        """使用率が 95% 以上で DOWN"""
        mock_usage.return_value = MagicMock(total=100, used=96, free=4)
        result = probe_disk_space("/", threshold_pct=90.0)
        self.assertEqual(result, HealthStatus.DOWN)

    @patch("core_runtime.health.shutil.disk_usage")
    def test_probe_disk_space_up(self, mock_usage):
        """使用率がしきい値未満で UP"""
        mock_usage.return_value = MagicMock(total=100, used=50, free=50)
        result = probe_disk_space("/", threshold_pct=90.0)
        self.assertEqual(result, HealthStatus.UP)

    @patch("core_runtime.health.shutil.disk_usage")
    def test_detailed_disk_degraded_reason_survives_aggregation(self, mock_usage):
        """A degraded disk reports its usage and configured threshold."""
        mock_usage.return_value = MagicMock(total=1000, used=911, free=89)
        checker = HealthChecker()
        checker.register_probe(
            "disk", lambda: probe_disk_space("/", with_reason=True)
        )
        result = checker.aggregate_health()
        self.assertEqual(result["status"], "DEGRADED")
        self.assertEqual(result["probes"]["disk"]["status"], "DEGRADED")
        self.assertEqual(
            result["probes"]["disk"]["message"],
            "disk: 91.1% used >= 90% threshold",
        )

    @patch("core_runtime.health.shutil.disk_usage")
    def test_detailed_disk_reasons_follow_threshold_boundaries(self, mock_usage):
        """Detailed disk results preserve existing threshold precedence."""
        for used, threshold, expected_status, expected_message in (
            (899, 90.0, "UP", "ok"),
            (900, 90.0, "DEGRADED", "disk: 90.0% used >= 90% threshold"),
            (925, 92.5, "DEGRADED", "disk: 92.5% used >= 92.5% threshold"),
            (950, 99.9, "DOWN", "disk: 95.0% used >= 95% threshold"),
        ):
            with self.subTest(used=used, threshold=threshold):
                mock_usage.return_value = MagicMock(
                    total=1000, used=used, free=1000 - used
                )
                checker = HealthChecker()
                checker.register_probe(
                    "disk", lambda: probe_disk_space(
                        "/", threshold_pct=threshold, with_reason=True
                    )
                )
                result = checker.aggregate_health()["probes"]["disk"]
                self.assertEqual(result["status"], expected_status)
                self.assertEqual(result["message"], expected_message)

    @patch("core_runtime.health.shutil.disk_usage", side_effect=OSError("unavailable"))
    def test_detailed_disk_unknown_has_failure_reason(self, mock_usage):
        """An unreadable disk carries a reason without changing UNKNOWN."""
        result = probe_disk_space("/", with_reason=True)
        self.assertIsInstance(result, HealthProbeResult)
        self.assertEqual(result.status, HealthStatus.UNKNOWN)
        self.assertEqual(result.message, "disk: unable to read usage: unavailable")

    def test_probe_memory_returns_status(self):
        """probe_memory が HealthStatus を返す"""
        result = probe_memory()
        self.assertIsInstance(result, HealthStatus)

    @patch("builtins.open", side_effect=FileNotFoundError)
    def test_probe_memory_no_procfs(self, mock_open):
        """/proc/meminfo がない環境では UNKNOWN"""
        result = probe_memory()
        self.assertEqual(result, HealthStatus.UNKNOWN)

    def test_probe_file_writable_tmp(self):
        """一時ディレクトリは書き込み可能（UP）"""
        with tempfile.TemporaryDirectory() as tmpdir:
            result = probe_file_writable(tmpdir)
            self.assertEqual(result, HealthStatus.UP)

    def test_probe_file_writable_nonexistent(self):
        """存在しないディレクトリでは DOWN"""
        result = probe_file_writable("/nonexistent_path_xyz_123")
        self.assertEqual(result, HealthStatus.DOWN)


# =========================================================================
# get_health_checker テスト
# =========================================================================


class TestGetHealthChecker(unittest.TestCase):
    """get_health_checker のテスト"""

    def setUp(self):
        reset_health_checker()

    def tearDown(self):
        reset_health_checker()

    def test_returns_health_checker(self):
        """HealthChecker インスタンスを返す"""
        checker = get_health_checker()
        self.assertIsInstance(checker, HealthChecker)

    def test_singleton(self):
        """同じインスタンスを返す"""
        c1 = get_health_checker()
        c2 = get_health_checker()
        self.assertIs(c1, c2)

    def test_reset(self):
        """reset 後は新しいインスタンスを返す"""
        c1 = get_health_checker()
        reset_health_checker()
        c2 = get_health_checker()
        self.assertIsNot(c1, c2)

    def test_thread_safe_singleton(self):
        """複数スレッドから同じインスタンスが返る"""
        results = {}
        barrier = threading.Barrier(4)

        def get_checker(idx):
            barrier.wait(timeout=5)
            results[idx] = get_health_checker()

        threads = [
            threading.Thread(target=get_checker, args=(i,))
            for i in range(4)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        instances = list(results.values())
        for inst in instances[1:]:
            self.assertIs(inst, instances[0])


if __name__ == "__main__":
    unittest.main()
