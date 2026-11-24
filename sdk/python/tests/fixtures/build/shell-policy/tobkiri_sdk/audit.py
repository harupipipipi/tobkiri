"""Append-only guest audit API; reading is independently permission-gated."""

import json
from typing import Any
from ._binding import invoke


def append(kind: str, payload: Any) -> None:
    """Append one bounded audit event; core supplies identity and the hash chain."""
    invoke("audit", "append", kind=kind,
           payload=json.dumps(payload, ensure_ascii=False, allow_nan=False))
