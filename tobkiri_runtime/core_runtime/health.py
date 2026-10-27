"""
health.py - ヘルスチェック基盤

Wave 13 T-051: ヘルスチェックエンドポイント基盤モジュール。
複数のプローブを登録し、タイムアウト付きで実行して
全体のヘルスステータスを集約する。

主要コンポーネント:
- HealthStatus: UP / DOWN / DEGRADED / UNKNOWN の列挙型
- HealthChecker: プローブ登録・実行・集約クラス
- probe_disk_space(): ディスク空き容量チェック
- probe_memory(): メモリ使用量チェック
- probe_file_writable(): ファイル書き込み可能チェック
- get_health_checker(): キャッシュ付きファクトリ関数
"""

from __future__ import annotations

import enum
import os
import shutil
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional


_this_module = sys.modules.get(__name__)
if _this_module is not None:
    if __name__.startswith("tobkiri_runtime."):
        sys.modules.setdefault(__name__.removeprefix("tobkiri_runtime."), _this_module)
    else:
        sys.modules.setdefault(f"tobkiri_runtime.{__name__}", _this_module)


# ============================================================
# HealthStatus
# ============================================================

class HealthStatus(enum.Enum):
    """ヘルスチェックの状態を表す列挙型。"""
    UP = "UP"
    DOWN = "DOWN"
    DEGRADED = "DEGRADED"
    UNKNOWN = "UNKNOWN"


# ============================================================
# HealthChecker
# ============================================================

# プローブ関数の型: 引数なしで HealthStatus を返す
ProbeFunc = Callable[[], HealthStatus]


class HealthChecker:
    """
    複数のヘルスチェックプローブを管理・実行するクラス。

    プローブを登録し、aggregate_health() で全プローブを
    タイムアウト付きで並行実行して結果を集約する。

    Usage:
        checker = HealthChecker()
        checker.register_probe("disk", lambda: probe_disk_space("/tmp"))
        result = checker.aggregate_health()
        # result = {"status": "UP", "timestamp": "...", "probes": {...}}
    """

    def __init__(self, default_timeout: float = 5.0) -> None:
        """
        Args:
            default_timeout: プローブ実行のデフォルトタイムアウト（秒）
        """
        self._probes: Dict[str, ProbeFunc] = {}
        self._lock = threading.Lock()
        self._default_timeout = default_timeout

    @property
    def default_timeout(self) -> float:
        """デフォルトタイムアウト値を返す。"""
        return self._default_timeout

    @default_timeout.setter
    def default_timeout(self, value: float) -> None:
        """デフォルトタイムアウト値を設定する。"""
        if value <= 0:
            raise ValueError("timeout must be positive")
        self._default_timeout = value

    def register_probe(self, name: str, func: ProbeFunc) -> None:
        """
        ヘルスチェックプローブを登録する。

        Args:
            name: プローブの一意な名前
            func: 引数なしで HealthStatus を返す関数
        """
        with self._lock:
            self._probes[name] = func

    def remove_probe(self, name: str) -> bool:
        """
        プローブを削除する。

        Args:
            name: 削除するプローブの名前

        Returns:
            削除に成功した場合 True、存在しなかった場合 False
        """
        with self._lock:
            if name in self._probes:
                del self._probes[name]
                return True
            return False

    def list_probes(self) -> List[str]:
        """登録済みプローブ名のリストを返す。"""
        with self._lock:
            return list(self._probes.keys())

    def _run_probe(
        self, name: str, func: ProbeFunc, timeout: float
    ) -> Dict[str, Any]:
        """
        単一プローブをタイムアウト付きで実行する。

        Returns:
            {"status": str, "message": str, "duration_ms": float}
        """
        start = time.monotonic()
        try:
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(func)
                status = future.result(timeout=timeout)
            elapsed_ms = (time.monotonic() - start) * 1000.0
            return {
                "status": status.value,
                "message": "ok",
                "duration_ms": round(elapsed_ms, 2),
            }
        except FuturesTimeoutError:
            elapsed_ms = (time.monotonic() - start) * 1000.0
            return {
                "status": HealthStatus.DOWN.value,
                "message": f"timeout after {timeout}s",
                "duration_ms": round(elapsed_ms, 2),
            }
        except Exception as exc:
            elapsed_ms = (time.monotonic() - start) * 1000.0
            return {
                "status": HealthStatus.DOWN.value,
                "message": f"error: {exc}",
                "duration_ms": round(elapsed_ms, 2),
            }

    def aggregate_health(
        self, timeout: Optional[float] = None
    ) -> Dict[str, Any]:
        """
        全プローブを実行し、結果を集約して JSON dict で返す。

        Args:
            timeout: プローブ実行タイムアウト（秒）。None の場合デフォルト値を使用。

        Returns:
            {
                "status": "UP" | "DOWN" | "DEGRADED" | "UNKNOWN",
                "timestamp": "ISO8601 UTC",
                "probes": {
                    "probe_name": {
                        "status": "UP",
                        "message": "ok",
                        "duration_ms": 1.23
                    },
                    ...
                }
            }
        """
        if timeout is None:
            timeout = self._default_timeout

        with self._lock:
            probes_snapshot = dict(self._probes)

        timestamp = datetime.now(timezone.utc).isoformat().replace(
            "+00:00", "Z"
        )

        if not probes_snapshot:
            return {
                "status": HealthStatus.UP.value,
                "timestamp": timestamp,
                "probes": {},
            }

        probe_results: Dict[str, Dict[str, Any]] = {}
        for name, func in probes_snapshot.items():
            probe_results[name] = self._run_probe(name, func, timeout)

        overall = self._determine_overall_status(probe_results)

        return {
            "status": overall.value,
            "timestamp": timestamp,
            "probes": probe_results,
        }

    @staticmethod
    def _determine_overall_status(
        probe_results: Dict[str, Dict[str, Any]]
    ) -> HealthStatus:
        """
        プローブ結果から全体のヘルスステータスを決定する。

        ルール:
        - 1つでも DOWN → DOWN
        - DOWN がなく 1つでも DEGRADED → DEGRADED
        - DOWN も DEGRADED もなく 1つでも UNKNOWN → DEGRADED
        - 全て UP → UP
        """
        statuses = [r["status"] for r in probe_results.values()]
        if HealthStatus.DOWN.value in statuses:
            return HealthStatus.DOWN
        if HealthStatus.DEGRADED.value in statuses:
            return HealthStatus.DEGRADED
        if HealthStatus.UNKNOWN.value in statuses:
            return HealthStatus.DEGRADED
        return HealthStatus.UP


# ============================================================
# 組み込みプローブ関数
# ============================================================

def probe_disk_space(
    path: str = "/", threshold_pct: float = 90.0
) -> HealthStatus:
    """
    ディスク空き容量をチェックするプローブ。

    使用率が threshold_pct 以上なら DEGRADED、
    95% 以上なら DOWN を返す。

    Args:
        path: チェック対象のパス
        threshold_pct: 警告しきい値（パーセント）

    Returns:
        HealthStatus
    """
    try:
        usage = shutil.disk_usage(path)
        used_pct = (usage.used / usage.total) * 100.0
        if used_pct >= 95.0:
            return HealthStatus.DOWN
        if used_pct >= threshold_pct:
            return HealthStatus.DEGRADED
        return HealthStatus.UP
    except Exception:
        return HealthStatus.UNKNOWN


def probe_memory(threshold_pct: float = 90.0) -> HealthStatus:
    """
    メモリ使用量をチェックするプローブ。

    /proc/meminfo を読み取り、使用率が threshold_pct 以上なら DEGRADED、
    95% 以上なら DOWN を返す。
    /proc/meminfo が利用できない環境では UNKNOWN を返す。

    Args:
        threshold_pct: 警告しきい値（パーセント）

    Returns:
        HealthStatus
    """
    try:
        meminfo: Dict[str, int] = {}
        with open("/proc/meminfo", "r") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2:
                    key = parts[0].rstrip(":")
                    value = int(parts[1])
                    meminfo[key] = value

        mem_total = meminfo.get("MemTotal", 0)
        mem_available = meminfo.get("MemAvailable", 0)

        if mem_total <= 0:
            return HealthStatus.UNKNOWN

        used_pct = ((mem_total - mem_available) / mem_total) * 100.0
        if used_pct >= 95.0:
            return HealthStatus.DOWN
        if used_pct >= threshold_pct:
            return HealthStatus.DEGRADED
        return HealthStatus.UP
    except Exception:
        return HealthStatus.UNKNOWN


def probe_file_writable(path: str) -> HealthStatus:
    """
    指定パスにファイルを書き込めるかチェックするプローブ。

    一時ファイルを作成して即削除する方法でテストする。

    Args:
        path: チェック対象のディレクトリパス

    Returns:
        HealthStatus
    """
    try:
        fd, tmp_path = tempfile.mkstemp(
            prefix=".health_check_", dir=path
        )
        try:
            os.write(fd, b"health_check")
        finally:
            os.close(fd)
            os.unlink(tmp_path)
        return HealthStatus.UP
    except Exception:
        return HealthStatus.DOWN


# ============================================================
# get_health_checker (ファクトリ関数)
# ============================================================

_health_checker_instance: Optional[HealthChecker] = None
_health_checker_lock = threading.Lock()


def get_health_checker() -> HealthChecker:
    """
    HealthChecker のファクトリ関数。
    シングルトンインスタンスを返す（キャッシュ付き）。

    Returns:
        HealthChecker インスタンス
    """
    global _health_checker_instance
    if _health_checker_instance is not None:
        return _health_checker_instance

    with _health_checker_lock:
        if _health_checker_instance is None:
            _health_checker_instance = HealthChecker()
        return _health_checker_instance


def reset_health_checker() -> None:
    """HealthChecker インスタンスをリセットする（テスト用）。"""
    global _health_checker_instance
    with _health_checker_lock:
        _health_checker_instance = None
