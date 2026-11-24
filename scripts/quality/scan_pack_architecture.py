#!/usr/bin/env python3
"""Repository-root entry point for the Wave 1 pack boundary scanner."""

from __future__ import annotations

import runpy
from pathlib import Path


if __name__ == "__main__":
    scanner = (
        Path(__file__).resolve().parents[2]
        / "tobkiri_runtime"
        / "scripts"
        / "quality"
        / "scan_pack_architecture.py"
    )
    runpy.run_path(str(scanner), run_name="__main__")
