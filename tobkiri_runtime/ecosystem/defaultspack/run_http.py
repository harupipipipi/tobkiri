"""Retired standalone Defaultspack HTTP entrypoint.

The launcher starts ``ecosystem.defaultspack.desktop_app``, which captures an
immutable Pack v4 activation and serves only Host-owned contract routes.  The
historical standalone transport exposed direct Flow execution and therefore
cannot participate in production startup.
"""

from __future__ import annotations

import sys


def main() -> int:
    """Fail closed instead of starting the pre-Broker HTTP transport."""

    print(
        "Defaultspack standalone HTTP is retired; launch Tobkiri Launcher.",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
