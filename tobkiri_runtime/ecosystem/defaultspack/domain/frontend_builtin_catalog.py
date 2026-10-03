"""Shared immutable built-in UI definitions; no user-state discovery."""

from __future__ import annotations

import json
from copy import deepcopy
from functools import lru_cache
from pathlib import Path
from typing import Any


@lru_cache(maxsize=1)
def _read_definitions() -> dict[str, Any]:
    path = Path(__file__).resolve().parents[1] / "defaultspack" / "builtin_ui_catalog.v1.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("built-in UI definitions must be an object")
    return value


def builtin_ui_catalog() -> dict[str, Any]:
    """Return detached definitions from the sealed Defaults source closure."""
    return deepcopy(_read_definitions())
