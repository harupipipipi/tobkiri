"""Canonical defaultspack operation implementation helpers."""

from .dispatcher import run_defaultspack_function
from .response import error, ok

__all__ = ["run_defaultspack_function", "ok", "error"]
