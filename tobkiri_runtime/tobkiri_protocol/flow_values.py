"""Portable value contracts for Pack-to-Pack Flow connections.

Values carry no authority, paths or URLs. Schema annotations name a value's
semantic type; the enclosing captured schema and normal Broker remain binding.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
from collections.abc import Mapping
from typing import Any

VALUE_TYPE_KEY = "x-tobkiri-value-type"
FLOW_ROLE_KEY = "x-tobkiri-flow-role"
SOUND_TYPE = "tobkiri.value.sound.v1"
MAX_SOUND_BYTES = 1024 * 1024
SOUND_MEDIA_TYPES = (
    "audio/mpeg", "audio/mp3", "audio/wav", "audio/x-wav", "audio/wave",
    "audio/mp4", "audio/m4a", "audio/aac", "audio/flac", "audio/ogg",
    "audio/webm", "audio/l16",
)
_TYPE_ID = re.compile(r"^[a-z][a-z0-9._-]{0,119}\.v[1-9][0-9]{0,3}$")


def value_type(schema: Mapping[str, Any]) -> str | None:
    """Read a bounded, versioned semantic type without consulting a registry."""
    raw = schema.get(VALUE_TYPE_KEY)
    return raw if isinstance(raw, str) and _TYPE_ID.fullmatch(raw) else None


def sound_schema() -> dict[str, Any]:
    """Return an independent inline Sound v1 schema for any Pack to embed."""
    return {
        "type": "object", "title": "sound", VALUE_TYPE_KEY: SOUND_TYPE,
        "additionalProperties": False,
        "required": ["content_id", "media_type", "data_base64", "byte_size"],
        "properties": {
            "content_id": {"type": "string", "maxLength": 71,
                           "pattern": "^sha256:[0-9a-f]{64}$"},
            "media_type": {"type": "string", "enum": list(SOUND_MEDIA_TYPES)},
            "data_base64": {"type": "string", "minLength": 4,
                            "maxLength": ((MAX_SOUND_BYTES + 2) // 3) * 4},
            "byte_size": {"type": "integer", "minimum": 1,
                          "maximum": MAX_SOUND_BYTES},
            "filename": {"type": "string", "minLength": 1, "maxLength": 255},
        },
    }


def validate_sound(value: Any) -> None:
    """Check bounded bytes, MIME, length and digest; never open a resource."""
    if not isinstance(value, Mapping):
        raise ValueError("sound must be an inline value")
    allowed = {"content_id", "media_type", "data_base64", "byte_size", "filename"}
    encoded = value.get("data_base64")
    size = value.get("byte_size")
    if (set(value) - allowed or value.get("media_type") not in SOUND_MEDIA_TYPES
            or type(size) is not int or not 1 <= size <= MAX_SOUND_BYTES
            or not isinstance(encoded, str)
            or not 4 <= len(encoded) <= ((MAX_SOUND_BYTES + 2) // 3) * 4):
        raise ValueError("sound fields are invalid")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        raise ValueError("sound encoding is invalid") from None
    if (len(raw) != size or base64.b64encode(raw).decode("ascii") != encoded
            or value.get("content_id") != "sha256:" + hashlib.sha256(raw).hexdigest()):
        raise ValueError("sound bytes, size and digest do not match")
    filename = value.get("filename")
    if filename is not None and (
        not isinstance(filename, str) or not 1 <= len(filename) <= 255
        or filename in {".", ".."} or any(ord(c) < 32 for c in filename)
        or "/" in filename or "\\" in filename
    ):
        raise ValueError("sound filename must be a display name")
