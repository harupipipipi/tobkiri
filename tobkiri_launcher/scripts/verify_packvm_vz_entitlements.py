#!/usr/bin/env python3
"""Verify the exact entitlement set carried by the PackVM VZ helper."""

from __future__ import annotations

import plistlib
import sys


EXPECTED_ENTITLEMENTS = {"com.apple.security.virtualization": True}


def verify_entitlements(payload: bytes) -> None:
    """Reject an invalid, missing, or over-privileged entitlement plist."""
    try:
        entitlements = plistlib.loads(payload)
    except (ValueError, plistlib.InvalidFileException) as exc:
        raise ValueError("PackVM VZ helper entitlement plist is invalid") from exc
    if entitlements != EXPECTED_ENTITLEMENTS:
        raise ValueError("PackVM VZ helper entitlements are not exact")


def main() -> int:
    """Read a codesign entitlement plist from stdin and verify it."""
    try:
        verify_entitlements(sys.stdin.buffer.read())
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
