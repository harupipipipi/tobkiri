#!/usr/bin/env python3
"""Verify the reviewed Pack-boundary baseline used before the base has one."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any


BASELINE_API_VERSION = "io.tobkiri.pack-boundary-baseline.v1"
POLICY = "exact-current-shrink-only-from-reference"
EXPECTED_REFERENCE_SHA256 = (
    "005c8a628c045f7b2bf0851c75fa19607f817cd06857871694e850cc10fa6124"
)
EXPECTED_REFERENCE_VIOLATION_COUNT = 163


def _load_baseline(path: Path) -> tuple[bytes, set[str]]:
    """Load the minimal baseline shape needed to establish its provenance."""
    payload = path.read_bytes()
    document: Any = json.loads(payload)
    expected_keys = {"baseline_api_version", "policy", "summary", "violations"}
    if not isinstance(document, dict) or set(document) != expected_keys:
        raise ValueError("Pack boundary bootstrap schema is invalid")
    if document["baseline_api_version"] != BASELINE_API_VERSION:
        raise ValueError("Pack boundary bootstrap API version is invalid")
    if document["policy"] != POLICY:
        raise ValueError("Pack boundary bootstrap policy is invalid")
    if not isinstance(document["summary"], dict):
        raise ValueError("Pack boundary bootstrap summary is invalid")
    violations = document["violations"]
    if not isinstance(violations, list):
        raise ValueError("Pack boundary bootstrap violations are invalid")
    fingerprints: list[str] = []
    for violation in violations:
        fingerprint = (
            violation.get("fingerprint") if isinstance(violation, dict) else None
        )
        if not isinstance(fingerprint, str) or not fingerprint:
            raise ValueError("Pack boundary bootstrap fingerprint is invalid")
        fingerprints.append(fingerprint)
    if len(set(fingerprints)) != len(fingerprints):
        raise ValueError("Pack boundary bootstrap fingerprints are not unique")
    return payload, set(fingerprints)


def verify_bootstrap(candidate: Path, reference: Path) -> None:
    """Require candidate violations to be a subset of an immutable baseline."""
    candidate_stat = candidate.stat()
    reference_stat = reference.stat()
    if candidate.resolve() == reference.resolve() or (
        candidate_stat.st_dev,
        candidate_stat.st_ino,
    ) == (reference_stat.st_dev, reference_stat.st_ino):
        raise ValueError("candidate cannot authorize its own bootstrap baseline")

    reference_payload, reference_fingerprints = _load_baseline(reference)
    if hashlib.sha256(reference_payload).hexdigest() != EXPECTED_REFERENCE_SHA256:
        raise ValueError("Pack boundary bootstrap reference digest mismatch")
    if len(reference_fingerprints) != EXPECTED_REFERENCE_VIOLATION_COUNT:
        raise ValueError("Pack boundary bootstrap reference violation count is invalid")

    _, candidate_fingerprints = _load_baseline(candidate)
    additions = candidate_fingerprints - reference_fingerprints
    if additions:
        raise ValueError(
            "Pack boundary bootstrap candidate expands the reviewed reference"
        )


def main() -> int:
    """Run the command-line bootstrap provenance verifier."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--reference", type=Path, required=True)
    arguments = parser.parse_args()
    try:
        verify_bootstrap(arguments.candidate, arguments.reference)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
