from __future__ import annotations

import contextlib
import io
import sys
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from backend_core.ecosystem.registry import PackInfo, Registry


def test_registry_load_logs_are_cp932_safe(tmp_path, monkeypatch):
    from tests.v4_batch_support import assert_legacy_registry_fails_closed

    assert_legacy_registry_fails_closed()
