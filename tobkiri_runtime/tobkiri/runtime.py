"""Fail-closed top-level entrypoint for the Pack v4 runtime.

The v4 Host cannot reconstruct authority from repository contents or a legacy
startup profile.  A Launcher must capture and inject a verified
``ProductionRuntimeV4``/``V4DispatchSession``.  Consequently the historical
``app.main`` composition root is deliberately not imported here.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence


def main(argv: Sequence[str] | None = None) -> int:
    """Expose health diagnostics and reject implicit runtime startup."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments == ["--health"]:
        from app import main as host_main

        return host_main(arguments)
    raise SystemExit(
        "Tobkiri requires a Launcher-injected Pack v4 activation snapshot"
    )


__all__ = ["main"]
