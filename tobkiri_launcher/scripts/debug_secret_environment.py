"""Narrow process-environment adapter for the isolated debug harness.

Keeping environment access here makes the privileged boundary explicit and
lets callers pass snapshots instead of reaching into global process state.
"""

from __future__ import annotations

import os
from typing import Mapping


def process_environment() -> Mapping[str, str]:
    """Return the live process environment for immediate read-only inspection."""

    return os.environ


def copy_process_environment() -> dict[str, str]:
    """Return a detached child-process environment snapshot."""

    return dict(os.environ)
