"""Pure public input validation, shared without importing the UI schema reader.

The old UI module reexports this exact function object. Its body and reserved
keys are preserved; the private module has no filesystem or renderer imports.
"""

from __future__ import annotations

import json
from typing import Any, Mapping

_RESERVED = frozenset(
    {
        "__proto__",
        "prototype",
        "constructor",
        "approved",
        "approval",
        "profile_id",
        "profile_revision",
        "activation_id",
        "plan_digest",
        "plan_hash",
        "principal_id",
        "owner_pack_id",
        "catalog_hash",
    }
)


def validate_public_input(
    value: Any, *, depth: int = 0, allow_domain_profile_ids: bool = False
) -> None:
    """Reject authority hints and oversized/non-JSON values at every depth."""
    if depth > 16:
        raise ValueError("view input is too deeply nested")
    if isinstance(value, Mapping):
        if len(value) > 64:
            raise ValueError("view input has too many keys")
        for key, item in value.items():
            if (
                not isinstance(key, str)
                or len(key) > 128
                or (
                    key in _RESERVED
                    and not (key == "profile_id" and depth > 0 and allow_domain_profile_ids)
                )
                or key.startswith("_")
            ):
                raise ValueError("view input contains a reserved key")
            validate_public_input(
                item,
                depth=depth + 1,
                allow_domain_profile_ids=allow_domain_profile_ids,
            )
    elif isinstance(value, list):
        if len(value) > 256:
            raise ValueError("view input contains too many items")
        for item in value:
            validate_public_input(
                item,
                depth=depth + 1,
                allow_domain_profile_ids=allow_domain_profile_ids,
            )
    elif isinstance(value, str):
        if len(value) > 16384:
            raise ValueError("view input string is too long")
    elif value is not None and not isinstance(value, (bool, int, float)):
        raise ValueError("view input is not JSON")
    if depth == 0:
        encoded = json.dumps(value, allow_nan=False, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > 65536:
            raise ValueError("view input is too large")


validate_public_input.__module__ = "ecosystem.defaultspack.defaultspack.v4_view_contract"
