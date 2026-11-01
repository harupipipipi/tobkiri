"""Finite web entry declarations carried by an Application's sealed map.

These declarations select a display implementation, never an operation grant.
The caller must verify the containing map's artifact and active Profile first.
"""

from __future__ import annotations

import re
from typing import Any

from .canonical import canonical_json
from .errors import ProtocolError
from .ids import validate_canonical_id

_ROUTE = re.compile(r"/(?:[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*)?")
_FIELDS = {"entry_id", "route", "match", "contribution_id", "implementation", "label"}


def validate_frontend_entries(value: Any) -> dict[str, Any]:
    """Validate a bounded declaration, including its explicit default entry."""
    if not isinstance(value, dict) or set(value) != {"default_entry_id", "entries"}:
        raise ProtocolError("Application frontend declaration fields are invalid")
    entries = value["entries"]
    if not isinstance(entries, list) or not 1 <= len(entries) <= 64:
        raise ProtocolError("Application frontend entries are invalid")
    identities: set[str] = set()
    contributions: set[str] = set()
    routes: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != _FIELDS:
            raise ProtocolError("Application frontend entry fields are invalid")
        for field in ("entry_id", "contribution_id", "implementation"):
            validate_canonical_id(entry[field], field=field)
        if (
            not isinstance(entry["route"], str)
            or len(entry["route"]) > 1024
            or _ROUTE.fullmatch(entry["route"]) is None
            or not isinstance(entry["match"], str)
            or entry["match"] not in {"exact", "subpath"}
            or (entry["match"] == "subpath" and entry["route"] == "/")
        ):
            raise ProtocolError("Application frontend route is invalid")
        if (
            entry["entry_id"] in identities
            or entry["contribution_id"] in contributions
            or entry["route"] in routes
        ):
            raise ProtocolError("Application frontend entry is ambiguous")
        label = entry["label"]
        if not isinstance(label, str) or not label.strip() or len(label) > 256:
            raise ProtocolError("Application frontend label is invalid")
        identities.add(entry["entry_id"])
        contributions.add(entry["contribution_id"])
        routes.add(entry["route"])
    for entry in entries:
        if entry["match"] == "subpath" and any(
            route.startswith(entry["route"].rstrip("/") + "/")
            for route in routes
            if route != entry["route"]
        ):
            raise ProtocolError("Application frontend routes overlap")
    if (
        not isinstance(value["default_entry_id"], str)
        or value["default_entry_id"] not in identities
    ):
        raise ProtocolError("Application frontend default entry is missing")
    if len(canonical_json(value)) > 64 * 1024:
        raise ProtocolError("Application frontend declaration exceeds its limit")
    return value
