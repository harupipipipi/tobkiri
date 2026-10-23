"""Fail-closed top-level entrypoint for the Pack v4 runtime.

The v4 Host cannot reconstruct authority from repository contents or a legacy
startup profile.  A Launcher must capture and inject a verified
``ProductionRuntimeV4``/``V4DispatchSession``.  Consequently the historical
``app.main`` composition root is deliberately not imported here.
"""

from __future__ import annotations

from collections.abc import Sequence


def main(argv: Sequence[str] | None = None) -> int:
    """Reject implicit startup that lacks a Launcher-captured v4 snapshot."""
    del argv
    raise SystemExit(
        "Tobkiri requires a Launcher-injected Pack v4 activation snapshot"
    )


__all__ = ["main"]
