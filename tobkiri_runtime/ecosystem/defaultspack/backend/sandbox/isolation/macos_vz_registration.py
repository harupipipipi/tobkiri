"""Bounded retention of authenticated registrations; never remove VM resources."""

from __future__ import annotations

from collections.abc import Callable, Mapping
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from typing import Any

from tobkiri_protocol.secure_persistence import SecureDirectory


MAX_REGISTRATIONS = 64
MAX_REGISTRATION_BYTES = 128 * 1024
_RECORD_NAME = re.compile(r"[0-9a-f]{64}\.json")


def prepare_storage_rebind(
    state_root: Path, state: Mapping[str, Any]
) -> dict[str, str | int] | None:
    """Describe device drift only; this is not authority to adopt the storage.

    The caller must authenticate the old state, verify its immutable image and
    assets, and obtain separate consent for the returned digest. Legacy records
    contain no volume UUID: matching paths/inodes alone do not prove continuity.
    No file is created, repaired, or removed by this check.
    """
    instance_root = state_root / "instances" / str(state["instance"])
    if (
        state.get("vz_state_root_digest")
        != "sha256:" + hashlib.sha256(str(state_root).encode()).hexdigest()
        or state.get("instance_root") != str(instance_root)
    ):
        raise ValueError("PackVM VZ storage re-registration cannot change paths")
    metadata = []
    for root, inode_key in (
        (state_root, "vz_state_root_inode"),
        (instance_root, "instance_root_inode"),
    ):
        if not root.is_absolute() or root.resolve(strict=True) != root:
            raise ValueError("PackVM VZ storage re-registration path is unsafe")
        info = root.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_mode & 0o077
            or info.st_uid != os.getuid()
            or type(state.get(inode_key)) is not int
            or state[inode_key] != info.st_ino
        ):
            raise ValueError("PackVM VZ storage re-registration directory changed")
        metadata.append(info)
    previous = state.get("vz_state_root_device")
    if (
        type(previous) is not int or previous <= 0
        or type(state.get("instance_root_device")) is not int
        or previous != state["instance_root_device"]
        or metadata[0].st_dev != metadata[1].st_dev
    ):
        raise ValueError("PackVM VZ storage re-registration requires one filesystem")
    if previous == metadata[0].st_dev:
        return None
    facts: dict[str, str | int] = {
        "previous_attestation_digest": state["attestation_digest"],
        "state_root": str(state_root),
        "instance_root": str(instance_root),
        "previous_device": previous,
        "current_device": metadata[0].st_dev,
        "state_root_inode": metadata[0].st_ino,
        "instance_root_inode": metadata[1].st_ino,
    }
    raw = json.dumps(facts, sort_keys=True, separators=(",", ":")).encode()
    return {**facts, "digest": "sha256:" + hashlib.sha256(raw).hexdigest()}


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
