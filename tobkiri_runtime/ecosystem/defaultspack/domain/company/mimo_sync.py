from __future__ import annotations

import threading
import time
from typing import Any

from core_runtime.di_container import get_container


MIMO_CODING_COMPANY_ID = "mimo-coding-company"
_MIN_SYNC_INTERVAL_SECONDS = 2.0
_lock = threading.Lock()
_last_sync_at = 0.0


def sync_mimo_company_workspace(
    company_id: str | None,
    *,
    force: bool = False,
    sync_observability: bool = True,
    include_desktop_monitoring: bool = False,
) -> dict[str, Any] | None:
    """Best-effort optional MiMo profile sync for Team Workspace reads."""
    global _last_sync_at

    if str(company_id or "").strip() != MIMO_CODING_COMPANY_ID:
        return None

    now = time.monotonic()
    if not force and now - _last_sync_at < _MIN_SYNC_INTERVAL_SECONDS:
        return {"status": "skipped", "reason": "throttled"}

    if not _lock.acquire(blocking=False):
        return {"status": "skipped", "reason": "in_progress"}

    try:
        session = get_container().get_or_none("v4_dispatch_session")
        if session is None:
            return {
                "status": "unavailable",
                "reason": "A captured v4 dispatch session is required.",
            }
        # The operations-company Pack is declarative-only in the v4 catalog;
        # there is no executable contract for this legacy status projection.
        # Keep the boundary fail-closed instead of importing its implementation.
        del session, sync_observability, include_desktop_monitoring
        _last_sync_at = time.monotonic()
        return {
            "status": "unavailable",
            "reason": "The operations-company status operation is metadata-only in v4.",
        }
    finally:
        _lock.release()
