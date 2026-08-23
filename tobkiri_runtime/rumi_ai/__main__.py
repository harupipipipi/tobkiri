"""Narrow compatibility entrypoint for canonical Host diagnostics."""

import sys
from collections.abc import Sequence

from tobkiri.runtime import main as runtime_main


def main(argv: Sequence[str] | None = None) -> int:
    """Expose canonical health while keeping implicit legacy startup closed."""

    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments == ["--health"]:
        from app import main as host_main

        return host_main(arguments)
    return runtime_main(arguments)


if __name__ == "__main__":
    raise SystemExit(main())
