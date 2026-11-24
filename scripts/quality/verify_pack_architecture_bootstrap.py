#!/usr/bin/env python3
"""Verify the one-time reviewed Pack architecture baseline bootstrap."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any


EXPECTED_REFERENCE_SHA256 = (
    "9a10c35337e97b5a1b554fe732b63de870877434a1963121e5efb1ca1304f40d"
)
EXPECTED_EXCEPTION_COUNT = 110


def _load_baseline(path: Path) -> tuple[bytes, dict[str, dict[str, Any]]]:
    """Load a strict, exact-identity architecture baseline document."""
    payload = path.read_bytes()
    document: Any = json.loads(payload)
    if not isinstance(document, dict) or set(document) != {
        "schema_version",
        "policy",
        "exceptions",
    }:
        raise ValueError("Pack architecture bootstrap schema is invalid")
    if document["schema_version"] != 2:
        raise ValueError("Pack architecture bootstrap schema version is invalid")
    if document["policy"] != "shrink_only_exact_edges":
        raise ValueError("Pack architecture bootstrap policy is invalid")
    exceptions = document["exceptions"]
    if not isinstance(exceptions, list):
        raise ValueError("Pack architecture bootstrap exceptions are invalid")
    by_identity: dict[str, dict[str, Any]] = {}
    for exception in exceptions:
        identity = exception.get("identity") if isinstance(exception, dict) else None
        if not isinstance(identity, str) or not identity:
            raise ValueError("Pack architecture bootstrap identity is invalid")
        if identity in by_identity:
            raise ValueError("Pack architecture bootstrap identities are not unique")
        by_identity[identity] = exception
    if len(by_identity) != len(exceptions):
        raise ValueError("Pack architecture bootstrap identities are not unique")
    return payload, by_identity


def verify_bootstrap(candidate: Path, reference: Path) -> None:
    """Require an exact-record candidate subset of an immutable reference."""
    candidate_stat = candidate.stat()
    reference_stat = reference.stat()
    if candidate.resolve() == reference.resolve() or (
        candidate_stat.st_dev,
        candidate_stat.st_ino,
    ) == (reference_stat.st_dev, reference_stat.st_ino):
        raise ValueError("candidate cannot authorize its own bootstrap baseline")
    reference_payload, reference_exceptions = _load_baseline(reference)
    if hashlib.sha256(reference_payload).hexdigest() != EXPECTED_REFERENCE_SHA256:
        raise ValueError("Pack architecture bootstrap reference digest mismatch")
    if len(reference_exceptions) != EXPECTED_EXCEPTION_COUNT:
        raise ValueError(
            "Pack architecture bootstrap reference exception count is invalid"
        )
    _, candidate_exceptions = _load_baseline(candidate)
    additions = set(candidate_exceptions) - set(reference_exceptions)
    if additions:
        raise ValueError(
            "Pack architecture bootstrap candidate expands the reviewed reference"
        )
    changed = [
        identity
        for identity, exception in candidate_exceptions.items()
        if exception != reference_exceptions[identity]
    ]
    if changed:
        raise ValueError(
            "Pack architecture bootstrap candidate modifies a reviewed exception"
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--reference", type=Path, required=True)
    arguments = parser.parse_args()
    try:
        verify_bootstrap(arguments.candidate, arguments.reference)
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
