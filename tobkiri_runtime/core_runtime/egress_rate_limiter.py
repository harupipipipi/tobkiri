"""
egress_rate_limiter.py - Pack別リクエストレート制限

スライディングウィンドウ方式のPack別レート制限。
egress_proxy.py から分離 (W13-T047)。
W12-T046 で追加。
"""
from __future__ import annotations

import collections
import os
import threading
import time
from typing import Dict, Tuple


# ============================================================
# レート制限定数
# ============================================================

try:
    DEFAULT_RATE_LIMIT_PER_MIN = int(os.environ.get("RUMI_EGRESS_RATE_LIMIT", "60"))
except (ValueError, TypeError):
    DEFAULT_RATE_LIMIT_PER_MIN = 60

RATE_LIMIT_WINDOW_SECONDS = 60.0


# ============================================================
# PackRateLimiter
# ============================================================

class PackRateLimiter:
    """
    Pack別のリクエストレート制限（スライディングウィンドウ方式）

    デフォルト: 60 req/min（RUMI_EGRESS_RATE_LIMIT で変更可能）
    スレッドセーフ。
    """

    def __init__(self, max_requests_per_min: int | None = None):
        self._max_rpm = max_requests_per_min if max_requests_per_min is not None else DEFAULT_RATE_LIMIT_PER_MIN
        self._windows: Dict[str, collections.deque] = {}
        self._lock = threading.Lock()

    def check_rate_limit(self, pack_id: str) -> Tuple[bool, str]:
        """
        レート制限チェック。許可されれば記録もする。

        Returns:
            (allowed, reason)
        """
        now = time.time()
        cutoff = now - RATE_LIMIT_WINDOW_SECONDS

        with self._lock:
            if pack_id not in self._windows:
                self._windows[pack_id] = collections.deque()

            window = self._windows[pack_id]

            while window and window[0] < cutoff:
                window.popleft()

            if len(window) >= self._max_rpm:
                return False, (
                    f"Rate limit exceeded: {len(window)}/{self._max_rpm} "
                    f"requests in {RATE_LIMIT_WINDOW_SECONDS}s window"
                )

            window.append(now)
            return True, ""

    def get_current_count(self, pack_id: str) -> int:
        """現在のウィンドウ内リクエスト数を取得"""
        now = time.time()
        cutoff = now - RATE_LIMIT_WINDOW_SECONDS

        with self._lock:
            window = self._windows.get(pack_id)
            if not window:
                return 0
            while window and window[0] < cutoff:
                window.popleft()
            return len(window)

    def reset(self, pack_id: str | None = None) -> None:
        """レート制限カウンターをリセット（テスト用）"""
        with self._lock:
            if pack_id:
                self._windows.pop(pack_id, None)
            else:
                self._windows.clear()
