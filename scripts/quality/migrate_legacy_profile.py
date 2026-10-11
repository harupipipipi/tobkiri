#!/usr/bin/env python3
"""Repository-root entry point for safe legacy profile migration."""

from __future__ import annotations

import runpy
from pathlib import Path


if __name__ == "__main__":
    migrator = (
        Path(__file__).resolve().parents[2]
        / "tobkiri_runtime"
        / "scripts"
        / "quality"
        / "migrate_legacy_profile.py"
    )
    runpy.run_path(str(migrator), run_name="__main__")
