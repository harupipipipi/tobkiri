"""Bounded retention of authenticated registrations; never remove VM resources."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
import re

from tobkiri_protocol.secure_persistence import SecureDirectory


MAX_REGISTRATIONS = 64
MAX_REGISTRATION_BYTES = 128 * 1024
_RECORD_NAME = re.compile(r"[0-9a-f]{64}\.json")


def retain_registration(
    state_root: Path,
    attestation_digest: str,
    raw: bytes,
    *,
    before_publish: Callable[[], None],
) -> None:
    """Retain an exact verified record while the caller holds its mutation gate."""
    name = attestation_digest.removeprefix("sha256:") + ".json"
    if not _RECORD_NAME.fullmatch(name) or len(raw) > MAX_REGISTRATION_BYTES:
        raise ValueError("PackVM VZ registration history record is invalid")
    history = state_root / "registration-history"
    storage = SecureDirectory(history, create=True)
    count = 0
    # Unknown entries also block retention: never delete or overwrite residue.
    for entry in history.iterdir():
        count += 1
        if count > MAX_REGISTRATIONS or not _RECORD_NAME.fullmatch(entry.name):
            raise ValueError("PackVM VZ registration history requires review")
        storage.read_bytes_bounded(entry.name, max_bytes=MAX_REGISTRATION_BYTES)
    if storage.exists(name):
        if storage.read_bytes_bounded(name, max_bytes=MAX_REGISTRATION_BYTES) != raw:
            raise ValueError("PackVM VZ retained registration changed")
        before_publish()
        return
    if count >= MAX_REGISTRATIONS:
        raise ValueError("PackVM VZ registration history is full; existing records retained")

    def require_absent() -> None:
        before_publish()
        if storage.exists(name):
            raise ValueError("PackVM VZ retained registration appeared before publication")

    storage.write_bytes_atomic(name, raw, before_publish=require_absent)
