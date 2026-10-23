"""Public build-only initializer inputs for the reviewed original-producer patch.

This module does not initialize Python or provide a runtime entropy source.
The patched producer must enforce these inputs before CPython starts.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import PurePosixPath
from typing import Any

DOMAIN = b"tobkiri/componentize-py/0.25.0/deterministic-preinit/v1\0"


def _validate(value: Any) -> None:
    if value is None or type(value) is bool:
        return
    if type(value) is int and abs(value) <= 9_007_199_254_740_991:
        return
    if type(value) is str:
        value.encode("utf-8", errors="strict")
        return
    if type(value) is list:
        for item in value:
            _validate(item)
        return
    if type(value) is dict and all(type(key) is str for key in value):
        for key, item in value.items():
            _validate(key)
            _validate(item)
        return
    raise ValueError("initializer plan requires strict finite canonical JSON values")


def canonical_plan(document: dict[str, Any]) -> bytes:
    """Encode the reviewed JSON subset used by StrictV1 declared build plans."""
    if type(document) is not dict:
        raise ValueError("declared build plan must be an object")
    _validate(document)
    return json.dumps(document, ensure_ascii=False, allow_nan=False,
                      separators=(",", ":"), sort_keys=True).encode("utf-8")


def initializer_inputs(document: dict[str, Any], source_date_epoch: int) -> dict:
    """Derive public seed/time/env values; never consume ambient state or entropy."""
    if type(source_date_epoch) is not int or not 0 <= source_date_epoch < 2**63:
        raise ValueError("SOURCE_DATE_EPOCH must be explicit nonnegative i64 seconds")
    # Time is part of the literal declaration, not an inherited environment value.
    declaration = {"plan": document, "source_date_epoch": str(source_date_epoch)}
    digest = hashlib.sha256(canonical_plan(declaration)).digest()
    seed = hashlib.sha256(DOMAIN + digest).digest()
    return {"format": "tobkiri.compiler-init-plan/1",
            "producer": "componentize-py/0.25.0",
            "declared_plan_sha256": digest.hex(),
            "public_build_seed_hex": seed.hex(),
            "source_date_epoch": str(source_date_epoch),
            "monotonic_initial_ns": "0", "monotonic_step_ns": "1000",
            "guest_environment": {"PYTHONHASHSEED": "0", "TZ": "UTC",
                                  "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
                                  "PYTHONDONTWRITEBYTECODE": "1"},
            "runtime_crypto_enabled": False}


def entry_metadata(destination: str, size: int, source_date_epoch: int,
                   *, directory: bool = False) -> dict:
    """Declare stable input metadata; the patch must replace host inode metadata."""
    path = PurePosixPath(destination)
    if (not destination or not path.parts or path.is_absolute() or ".." in path.parts
            or destination != path.as_posix() or "\\" in destination):
        raise ValueError("input entry requires a normalized relative destination")
    if type(size) is not int or not 0 <= size <= 2**64-1:
        raise ValueError("input size must be a nonnegative u64")
    if type(source_date_epoch) is not int or not 0 <= source_date_epoch < 2**63:
        raise ValueError("SOURCE_DATE_EPOCH must be explicit nonnegative i64 seconds")
    identity = hashlib.sha256(b"tobkiri/preinit-fs-entry/v1\0" + destination.encode()).digest()
    return {"path": destination, "size": str(size),
            "mode": "0755" if directory else "0444",
            "mtime_seconds": str(source_date_epoch),
            "metadata_hash_hex": identity[:16].hex()}
