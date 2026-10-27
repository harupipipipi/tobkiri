"""Authenticated intent for completing a confirmed Host Profile publication.

This journal grants no activation authority. Recovery must independently verify
its exact candidate through the application's activation store and publish only
with the recorded Host-global predecessor's compare-and-swap fence.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from tobkiri_protocol.canonical import canonical_digest, canonical_json, strict_loads
from tobkiri_protocol.secure_persistence import SecureDirectory

from ..active_profile_store_v4 import ActiveProfilePointer, exclusive_profile_lock
from ..hmac_key_manager import (
    compute_data_hmac,
    generate_or_load_signing_key,
    load_signing_key,
    verify_data_hmac,
)

_SCHEMA = "io.tobkiri.profile-publication-intent.v1"
_LOCKED: ContextVar[tuple[int, int, Path] | None] = ContextVar(
    "profile_publication_lock", default=None
)


class ProfilePublicationJournal:
    """Serialize and authenticate one unfinished bootstrap publication."""

    filename = "bootstrap-publication.json"

    def __init__(self, user_data: Path) -> None:
        self.root = Path(user_data) / "profiles"
        self.path = self.root / self.filename

    def exists(self) -> bool:
        """Include unsafe paths so readers fail closed instead of ignoring them."""
        return self.path.exists() or self.path.is_symlink()

    @contextmanager
    def locked(self) -> Iterator[None]:
        """Hold the bounded process and OS lock, permitting same-thread nesting."""
        identity = (os.getpid(), threading.get_ident(), self.root.absolute())
        if _LOCKED.get() == identity:
            yield
            return
        directory = SecureDirectory(self.root, create=True)
        with exclusive_profile_lock(
            directory,
            ".bootstrap-publication.lock",
            timeout_seconds=30.0,
            monotonic_clock=time.monotonic,
            retry_sleep=time.sleep,
        ):
            token = _LOCKED.set(identity)
            try:
                yield
            finally:
                _LOCKED.reset(token)

    def read(self) -> dict[str, Any] | None:
        """Read an exact, authenticated intent; never create a missing read key."""
        directory = SecureDirectory(self.root, create=True)
        if not directory.exists(self.filename):
            return None
        envelope = strict_loads(directory.read_bytes(self.filename))
        key_path = self.root / ".publication-key"
        if not key_path.exists():
            raise ValueError("Profile publication signing key is unavailable")
        if not isinstance(envelope, dict) or set(envelope) != {"payload", "hmac"}:
            raise ValueError("Profile publication intent is invalid")
        payload = envelope["payload"]
        if (
            not isinstance(payload, dict)
            or set(payload) != {"schema", "predecessor", "candidate", "confirmation_digest"}
            or payload["schema"] != _SCHEMA
            or not isinstance(envelope["hmac"], str)
            or not verify_data_hmac(load_signing_key(key_path), payload, envelope["hmac"])
        ):
            raise ValueError("Profile publication intent authentication failed")
        if payload["predecessor"] is not None:
            ActiveProfilePointer.from_mapping(payload["predecessor"])
        return payload

    def begin(
        self,
        *,
        predecessor: ActiveProfilePointer | None,
        resolved: Any,
        activation_id: str,
        confirmation: Mapping[str, Any],
    ) -> None:
        """Durably record an already-checked confirmation before activation writes."""
        directory = SecureDirectory(self.root, create=True)
        if directory.exists(self.filename):
            raise ValueError("Profile publication already has an unfinished intent")
        payload = {
            "schema": _SCHEMA,
            "predecessor": predecessor.to_dict() if predecessor else None,
            "candidate": {
                "profile_id": resolved.profile["profile_id"],
                "profile_revision": resolved.plan["profile_revision"],
                "activation_id": activation_id,
                "plan_digest": resolved.plan["plan_digest"],
                "lock_digest": resolved.lock["lock_digest"],
                "profile_definition_digest": resolved.plan["profile_definition_digest"],
                "security_epoch": resolved.plan["security_epoch"],
            },
            "confirmation_digest": canonical_digest(dict(confirmation)),
        }
        key = generate_or_load_signing_key(self.root / ".publication-key")
        # The key utility creates secure material; flush it before the durable
        # intent can refer to it. The following atomic write fsyncs this directory.
        descriptor = directory.open_lock(".publication-key")
        try:
            directory.validate_open_file(".publication-key", descriptor)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        if load_signing_key(self.root / ".publication-key") != key:
            raise ValueError("Profile publication signing key changed")
        directory.write_bytes_atomic(
            self.filename,
            canonical_json({"payload": payload, "hmac": compute_data_hmac(key, payload)}) + b"\n",
        )

    def clear(self) -> None:
        """Remove an intent only while its publication lock remains held."""
        SecureDirectory(self.root, create=True).unlink(self.filename, missing_ok=True)


def candidate_matches(active: Any, candidate: Mapping[str, Any]) -> bool:
    """Compare all confirmed candidate identities after independent verification."""
    return candidate == {
        "profile_id": active.resolved.profile["profile_id"],
        "profile_revision": active.resolved.plan["profile_revision"],
        "activation_id": active.activation["activation_id"],
        "plan_digest": active.resolved.plan["plan_digest"],
        "lock_digest": active.resolved.lock["lock_digest"],
        "profile_definition_digest": active.resolved.plan["profile_definition_digest"],
        "security_epoch": active.resolved.plan["security_epoch"],
    }


def published_candidate(pointer: ActiveProfilePointer | None, intent: Mapping[str, Any]) -> bool:
    """Recognize only the immediately published candidate, never an ABA selection."""
    candidate = intent["candidate"]
    predecessor = intent["predecessor"]
    return pointer is not None and (
        pointer.identity()
        == tuple(
            candidate[key]
            for key in (
                "profile_id",
                "profile_revision",
                "activation_id",
                "plan_digest",
                "lock_digest",
            )
        )
        and pointer.generation == (predecessor["generation"] + 1 if predecessor else 1)
    )
