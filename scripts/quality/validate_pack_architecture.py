#!/usr/bin/env python3
"""Repository-root entry point for v4 Pack Architecture validation."""

from __future__ import annotations

import runpy
from pathlib import Path


if __name__ == "__main__":
    validator = (
        Path(__file__).resolve().parents[2]
        / "tobkiri_runtime"
        / "scripts"
        / "quality"
        / "validate_pack_architecture.py"
    )
    runpy.run_path(str(validator), run_name="__main__")
