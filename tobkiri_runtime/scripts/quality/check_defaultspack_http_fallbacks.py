#!/usr/bin/env python3
"""Validate that defaultspack compatibility HTTP routes are explicitly owned."""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"
for path in (str(ROOT), str(DEFAULTSPACK_ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

from transport.registry import (  # noqa: E402
    canonical_http_route_specs,
    require_legacy_route_allowlisted,
)


def main() -> int:
    legacy_specs = [
        spec
        for spec in canonical_http_route_specs(include_always_available=False)
        if spec.legacy_block_module
    ]
    failures: list[str] = []
    validated = 0
    for spec in legacy_specs:
        try:
            require_legacy_route_allowlisted(spec)
        except ValueError as exc:
            failures.append(str(exc))
        else:
            validated += 1

    if failures:
        print("defaultspack legacy HTTP fallback validation failed:", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        return 1

    print(f"validated {validated} allowlisted defaultspack HTTP fallbacks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
