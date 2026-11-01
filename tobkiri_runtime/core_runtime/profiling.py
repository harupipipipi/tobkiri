"""
profiling.py - パフォーマンスプロファイリング基盤

Wave 14 T-052: パフォーマンスプロファイリングの基盤モジュール。
コンテキストマネージャおよびデコレータで区間計測を行い、
呼出回数・合計時間・平均・最小・最大・パーセンタイル(p50/p95/p99)
の統計を収集する。メモリ制限付き（各区間の最新 N 件のみ保持）。

主要コンポーネント:
- Profiler: プロファイリングクラス
  - profile(): コンテキストマネージャ型区間計測
  - profile_func: 同期関数デコレータ
  - profile_async: 非同期関数デコレータ
  - get_stats(): 単一区間の統計取得
  - report_dict(): 全区間の統計を dict で取得
  - report(): テキスト形式のレポート生成
  - sections(): 登録済み区間名リスト
  - clear(): 全データクリア
- get_profiler(): キャッシュ付きファクトリ関数
- reset_profiler(): シングルトンリセット（テスト用）
"""

from __future__ import annotations

import functools
import math
import sys
import threading
import time
from collections import deque
from contextlib import contextmanager
from typing import Any, Callable, Dict, Generator, List, Optional


_this_module = sys.modules.get(__name__)
if _this_module is not None:
    if __name__.startswith("tobkiri_runtime."):
        sys.modules.setdefault(__name__.removeprefix("tobkiri_runtime."), _this_module)
    else:
        sys.modules.setdefault(f"tobkiri_runtime.{__name__}", _this_module)


# ============================================================
# Profiler
# ============================================================


class Profiler:
    """
    スレッドセーフなパフォーマンスプロファイリングクラス。

    コンテキストマネージャ、デコレータ（sync/async）で区間計測を行い、
    統計情報を収集する。各区間の計測サンプルは最新 max_samples 件のみ
    保持し、メモリ使用量を制限する。ただし呼出回数・合計時間・
    最小時間・最大時間はサンプル数に関わらず正確に累積される。

    Usage:
        profiler = Profiler()

        # コンテキストマネージャ
        with profiler.profile("section_a"):
            do_something()

        # 同期デコレータ
        @profiler.profile_func
        def my_func():
            pass

        # 非同期デコレータ
        @profiler.profile_async
        async def my_async_func():
            pass

        # 統計取得
        stats = profiler.get_stats("section_a")
        report = profiler.report()
    """

    def __init__(self, max_samples: int = 1000) -> None:
        """
        Args:
            max_samples: 各区間で保持する最大サンプル数（デフォルト 1000）
        """
        if max_samples < 1:
            raise ValueError("max_samples must be at least 1")
        self._max_samples = max_samples
        self._lock = threading.Lock()
        self._sections: Dict[str, Dict[str, Any]] = {}

    @property
    def max_samples(self) -> int:
        """各区間の最大サンプル数を返す。"""
        return self._max_samples

    def _ensure_section(self, name: str) -> Dict[str, Any]:
        """区間が存在しなければ初期化する。_lock 保持下で呼ぶこと。"""
        if name not in self._sections:
            self._sections[name] = {
                "samples": deque(maxlen=self._max_samples),
                "count": 0,
                "total_time": 0.0,
                "min_time": float("inf"),
                "max_time": 0.0,
            }
        return self._sections[name]

    def _record(self, name: str, elapsed: float) -> None:
        """計測結果を記録する。"""
        elapsed = max(float(elapsed), 1e-12)
        with self._lock:
            section = self._ensure_section(name)
            section["samples"].append(elapsed)
            section["count"] += 1
            section["total_time"] += elapsed
            if elapsed < section["min_time"]:
                section["min_time"] = elapsed
            if elapsed > section["max_time"]:
                section["max_time"] = elapsed

    @contextmanager
    def profile(self, name: str) -> Generator[None, None, None]:
        """
        区間計測を行うコンテキストマネージャ。

        例外が発生しても計測結果は記録される。

        Args:
            name: 区間名

        Usage:
            with profiler.profile("my_section"):
                do_work()
        """
        start = time.perf_counter()
        try:
            yield
        finally:
            elapsed = time.perf_counter() - start
            self._record(name, elapsed)

    def profile_func(self, func: Callable[..., Any]) -> Callable[..., Any]:
        """
        同期関数の実行時間を自動計測するデコレータ。

        区間名には func.__qualname__ を使用する。

        Usage:
            @profiler.profile_func
            def my_function():
                pass
        """
        section_name = func.__qualname__

        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            with self.profile(section_name):
                return func(*args, **kwargs)

        return wrapper

    def profile_async(self, func: Callable[..., Any]) -> Callable[..., Any]:
        """
        非同期関数の実行時間を自動計測するデコレータ。

        区間名には func.__qualname__ を使用する。

        Usage:
            @profiler.profile_async
            async def my_async_function():
                pass
        """
        section_name = func.__qualname__

        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            start = time.perf_counter()
            try:
                return await func(*args, **kwargs)
            finally:
                elapsed = time.perf_counter() - start
                self._record(section_name, elapsed)

        return wrapper

    @staticmethod
    def _percentile(sorted_data: List[float], p: float) -> float:
        """
        nearest-rank 方式でパーセンタイルを計算する。

        Args:
            sorted_data: ソート済みリスト（空でないこと）
            p: パーセンタイル値（0-100）

        Returns:
            パーセンタイル値
        """
        n = len(sorted_data)
        idx = math.ceil(p / 100.0 * n) - 1
        idx = max(0, min(idx, n - 1))
        return sorted_data[idx]

    def get_stats(self, name: str) -> Optional[Dict[str, Any]]:
        """
        指定区間の統計情報を返す。

        Args:
            name: 区間名

        Returns:
            統計情報の dict。区間が存在しない場合は None。
            {
                "name": str,
                "count": int,
                "total_time": float,
                "avg_time": float,
                "min_time": float,
                "max_time": float,
                "p50": float,
                "p95": float,
                "p99": float,
            }
        """
        with self._lock:
            if name not in self._sections:
                return None
            section = self._sections[name]
            count = section["count"]
            if count == 0:
                return {
                    "name": name,
                    "count": 0,
                    "total_time": 0.0,
                    "avg_time": 0.0,
                    "min_time": 0.0,
                    "max_time": 0.0,
                    "p50": 0.0,
                    "p95": 0.0,
                    "p99": 0.0,
                }
            sorted_samples = sorted(section["samples"])
            return {
                "name": name,
                "count": count,
                "total_time": section["total_time"],
                "avg_time": section["total_time"] / count,
                "min_time": section["min_time"],
                "max_time": section["max_time"],
                "p50": self._percentile(sorted_samples, 50),
                "p95": self._percentile(sorted_samples, 95),
                "p99": self._percentile(sorted_samples, 99),
            }

    def sections(self) -> List[str]:
        """登録済み区間名のリストを返す。"""
        with self._lock:
            return list(self._sections.keys())

    def report_dict(self) -> Dict[str, Any]:
        """
        全区間の統計情報を dict で返す。

        Returns:
            {
                "sections": {
                    "section_name": { ... stats ... },
                    ...
                }
            }
        """
        with self._lock:
            names = list(self._sections.keys())

        result: Dict[str, Any] = {"sections": {}}
        for name in names:
            stats = self.get_stats(name)
            if stats is not None:
                result["sections"][name] = stats
        return result

    def report(self) -> str:
        """
        テキスト形式のプロファイルレポートを生成する。

        Returns:
            人間が読みやすいテキストレポート
        """
        data = self.report_dict()
        sections_data = data["sections"]

        if not sections_data:
            return "Performance Profile Report\n==========================\n(no data)"

        lines: List[str] = [
            "Performance Profile Report",
            "==========================",
        ]
        for name in sorted(sections_data.keys()):
            stats = sections_data[name]
            lines.append(f"Section: {name}")
            lines.append(f"  Count:     {stats['count']}")
            lines.append(f"  Total:     {stats['total_time']:.6f}s")
            lines.append(f"  Avg:       {stats['avg_time']:.6f}s")
            lines.append(f"  Min:       {stats['min_time']:.6f}s")
            lines.append(f"  Max:       {stats['max_time']:.6f}s")
            lines.append(f"  P50:       {stats['p50']:.6f}s")
            lines.append(f"  P95:       {stats['p95']:.6f}s")
            lines.append(f"  P99:       {stats['p99']:.6f}s")

        return "\n".join(lines)

    def clear(self) -> None:
        """全計測データをクリアする。"""
        with self._lock:
            self._sections.clear()


# ============================================================
# get_profiler / reset_profiler (ファクトリ関数)
# ============================================================

_profiler_instance: Optional[Profiler] = None
_profiler_lock = threading.Lock()


def get_profiler() -> Profiler:
    """
    Profiler のファクトリ関数。
    シングルトンインスタンスを返す（キャッシュ付き）。

    Returns:
        Profiler インスタンス
    """
    global _profiler_instance
    if _profiler_instance is not None:
        return _profiler_instance

    with _profiler_lock:
        if _profiler_instance is None:
            _profiler_instance = Profiler()
        return _profiler_instance


def reset_profiler() -> None:
    """Profiler インスタンスをリセットする（テスト用）。"""
    global _profiler_instance
    with _profiler_lock:
        _profiler_instance = None
