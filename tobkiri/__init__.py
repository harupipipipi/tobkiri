"""Repository entrypoint for the canonical Tobkiri package.

Keep the implementation in ``tobkiri_runtime/tobkiri``. Extending this package's
search path makes its CLI and runtime modules available from the checkout
without shadowing them or changing the process-wide module search path.
"""

from pathlib import Path

__path__.append(str(Path(__file__).resolve().parents[1] / "tobkiri_runtime" / "tobkiri"))

from ._version import resolve_version  # noqa: E402

__version__ = resolve_version()
