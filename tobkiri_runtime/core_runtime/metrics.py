"""
metrics.py - メトリクス収集基盤

Wave 13 T-051: メトリクス収集の基盤モジュール。
カウンター、ゲージ、ヒストグラム、タイマーを提供し、
スレッドセーフにメトリクスを収集する。

主要コンポーネント:
- MetricsCollector: メトリクス収集クラス
  - increment(): カウンター増加
  - set_gauge(): ゲージ設定
  - observe(): ヒストグラム観測
  - timer(): コンテキストマネージャ型タイマー
  - snapshot(): 全メトリクスのスナップショット
  - reset(): 全メトリクスクリア
- get_metrics_collector(): キャッシュ付きファクトリ関数
"""

from __future__ import annotations

import threading
import time
import sys
from contextlib import contextmanager
from typing import Any, Dict, Generator, List, Optional, Tuple


_this_module = sys.modules.get(__name__)
if _this_module is not None:
    if __name__.startswith("tobkiri_runtime."):
        sys.modules.setdefault(__name__.removeprefix("tobkiri_runtime."), _this_module)
    else:
        sys.modules.setdefault(f"tobkiri_runtime.{__name__}", _this_module)


# ============================================================
# MetricsCollector
# ============================================================

# labels を内部キーに変換するためのヘルパー
def _normalize_labels(labels: Optional[Dict[str, str]]) -> Tuple[Tuple[str, str], ...]:
    """labels dict を hashable なタプルに変換する。"""
    if not labels:
        return ()
    return tuple(sorted(labels.items()))


def _labels_to_dict(key: Tuple[Tuple[str, str], ...]) -> Dict[str, str]:
    """内部キーを labels dict に戻す。"""
    return dict(key)


class MetricsCollector:
    """
    スレッドセーフなメトリクス収集クラス。

    カウンター、ゲージ、ヒストグラム、タイマーを提供する。
    各メトリクスは名前とオプションのラベルで識別される。

    Usage:
        collector = MetricsCollector()
        collector.increment("requests_total", labels={"method": "GET"})
        collector.set_gauge("active_connections", 42)
        collector.observe("response_time", 0.125, labels={"endpoint": "/api"})
        with collector.timer("process_duration"):
            do_something()
        snap = collector.snapshot()
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: Dict[str, Dict[Tuple[Tuple[str, str], ...], float]] = {}
        self._gauges: Dict[str, Dict[Tuple[Tuple[str, str], ...], float]] = {}
        self._histograms: Dict[str, Dict[Tuple[Tuple[str, str], ...], List[float]]] = {}

    def increment(
        self,
        name: str,
        labels: Optional[Dict[str, str]] = None,
        value: float = 1,
    ) -> None:
        """
        カウンターを増加させる。

        Args:
            name: メトリクス名
            labels: ラベル辞書
            value: 増加量（デフォルト 1）
        """
        if value < 0:
            raise ValueError("counter increment value must be non-negative")
        key = _normalize_labels(labels)
        with self._lock:
            if name not in self._counters:
                self._counters[name] = {}
            bucket = self._counters[name]
            bucket[key] = bucket.get(key, 0.0) + value

    def set_gauge(
        self,
        name: str,
        value: float,
        labels: Optional[Dict[str, str]] = None,
    ) -> None:
        """
        ゲージの値を設定する。

        Args:
            name: メトリクス名
            value: 設定値
            labels: ラベル辞書
        """
        key = _normalize_labels(labels)
        with self._lock:
            if name not in self._gauges:
                self._gauges[name] = {}
            self._gauges[name][key] = value

    def observe(
        self,
        name: str,
        value: float,
        labels: Optional[Dict[str, str]] = None,
    ) -> None:
        """
        ヒストグラムに値を観測する。

        Args:
            name: メトリクス名
            value: 観測値
            labels: ラベル辞書
        """
        key = _normalize_labels(labels)
        with self._lock:
            if name not in self._histograms:
                self._histograms[name] = {}
            bucket = self._histograms[name]
            if key not in bucket:
                bucket[key] = []
            bucket[key].append(value)

    @contextmanager
    def timer(
        self,
        name: str,
        labels: Optional[Dict[str, str]] = None,
    ) -> Generator[None, None, None]:
        """
        処理時間を計測してヒストグラムに記録するコンテキストマネージャ。

        計測結果は秒単位で observe() に渡される。

        Args:
            name: メトリクス名
            labels: ラベル辞書

        Usage:
            with collector.timer("process_time"):
                do_work()
        """
        start = time.monotonic()
        try:
            yield
        finally:
            elapsed = time.monotonic() - start
            self.observe(name, elapsed, labels)

    def snapshot(self) -> Dict[str, Any]:
        """
        全メトリクスのスナップショットを返す。

        Returns:
            {
                "counters": {
                    "name": [
                        {"labels": {...}, "value": 123.0},
                        ...
                    ]
                },
                "gauges": {
                    "name": [
                        {"labels": {...}, "value": 42.0},
                        ...
                    ]
                },
                "histograms": {
                    "name": [
                        {
                            "labels": {...},
                            "count": 10,
                            "sum": 1.5,
                            "min": 0.01,
                            "max": 0.5,
                            "avg": 0.15
                        },
                        ...
                    ]
                }
            }
        """
        with self._lock:
            result: Dict[str, Any] = {
                "counters": {},
                "gauges": {},
                "histograms": {},
            }

            for name, counter_buckets in self._counters.items():
                counter_entries: list[Dict[str, Any]] = []
                for key, value in counter_buckets.items():
                    counter_entries.append({
                        "labels": _labels_to_dict(key),
                        "value": value,
                    })
                result["counters"][name] = counter_entries

            for name, gauge_buckets in self._gauges.items():
                gauge_entries: list[Dict[str, Any]] = []
                for key, value in gauge_buckets.items():
                    gauge_entries.append({
                        "labels": _labels_to_dict(key),
                        "value": value,
                    })
                result["gauges"][name] = gauge_entries

            for name, histogram_buckets in self._histograms.items():
                histogram_entries: list[Dict[str, Any]] = []
                for key, values in histogram_buckets.items():
                    if values:
                        entry: Dict[str, Any] = {
                            "labels": _labels_to_dict(key),
                            "count": len(values),
                            "sum": sum(values),
                            "min": min(values),
                            "max": max(values),
                            "avg": sum(values) / len(values),
                        }
                    else:
                        entry = {
                            "labels": _labels_to_dict(key),
                            "count": 0,
                            "sum": 0.0,
                            "min": 0.0,
                            "max": 0.0,
                            "avg": 0.0,
                        }
                    histogram_entries.append(entry)
                result["histograms"][name] = histogram_entries

            return result

    def reset(self) -> None:
        """全メトリクスをクリアする。"""
        with self._lock:
            self._counters.clear()
            self._gauges.clear()
            self._histograms.clear()


# ============================================================
# get_metrics_collector (ファクトリ関数)
# ============================================================

_metrics_collector_instance: Optional[MetricsCollector] = None
_metrics_collector_lock = threading.Lock()


def get_metrics_collector() -> MetricsCollector:
    """
    MetricsCollector のファクトリ関数。
    シングルトンインスタンスを返す（キャッシュ付き）。

    Returns:
        MetricsCollector インスタンス
    """
    global _metrics_collector_instance
    if _metrics_collector_instance is not None:
        return _metrics_collector_instance

    with _metrics_collector_lock:
        if _metrics_collector_instance is None:
            _metrics_collector_instance = MetricsCollector()
        return _metrics_collector_instance


def reset_metrics_collector() -> None:
    """MetricsCollector インスタンスをリセットする（テスト用）。"""
    global _metrics_collector_instance
    with _metrics_collector_lock:
        _metrics_collector_instance = None
