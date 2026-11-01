"""One-use, exact-artifact publisher key reviews owned by the Native Host.

Only the private Launcher-authenticated HTTP boundary may call this service.
The challenge is deliberately process-local: restart or capture replacement
invalidates pending reviews, and client approval booleans are never accepted.
"""

from __future__ import annotations

import hmac
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from tobkiri_protocol.canonical import canonical_bytes, canonical_digest

from .pack_artifact_integrity import read_host_policy_snapshot
from .publisher_key_versions import (
    DOMAIN,
    PublisherKeyVersionPlan,
    is_legacy_publisher_key,
    prepare_publisher_key_version,
    resolve_publisher_key,
    write_publisher_key_version,
)

SCHEMA = "io.tobkiri.native-pack-key-version.v1"
_CONTEXT_FIELDS = {
    "host_owner_id",
    "profile_revision",
    "plan_digest",
    "activation_id",
    "security_epoch",
    "fencing_token",
}
ContextLoader = Callable[[], Mapping[str, Any]]


def _context(loader: ContextLoader) -> dict[str, Any]:
    value = dict(loader())
    if set(value) != _CONTEXT_FIELDS:
        raise ValueError("Native publisher review context is incomplete")
    for name in ("host_owner_id", "profile_revision", "plan_digest"):
        text = value[name]
        if (
            not isinstance(text, str)
            or len(text) != 71
            or not text.startswith("sha256:")
            or any(char not in "0123456789abcdef" for char in text[7:])
        ):
            raise ValueError("Native publisher review context is invalid")
    activation = value["activation_id"]
    if (
        not isinstance(activation, str)
        or not activation
        or len(activation) > 256
        or any(ord(char) < 32 for char in activation)
    ):
        raise ValueError("Native publisher review activation is invalid")
    if any(
        type(value[name]) is not int or value[name] < 1
        for name in ("security_epoch", "fencing_token")
    ):
        raise ValueError("Native publisher review fence is invalid")
    return value


@dataclass(frozen=True)
class _PendingReview:
    plan: PublisherKeyVersionPlan
    preview_bytes: bytes
    section_bytes: bytes
    request_id: str
    issued_at_ms: int
    expires_at_ms: int


class NativePackKeyVersionCeremony:
    """Keep bounded reviews until the same Native confirms their exact facts."""

    def __init__(
        self,
        *,
        clock_ms: Callable[[], int] = lambda: time.time_ns() // 1_000_000,
    ) -> None:
        self._clock_ms = clock_ms
        self._lock = threading.Lock()
        self._pending: dict[str, _PendingReview] = {}

    def prepare(
        self,
        preview: Mapping[str, Any],
        *,
        trust_store_path: Path,
        public_key_pem: str,
        signed_manifest_digest: str,
        compatibility_digest: str,
        context_loader: ContextLoader,
    ) -> dict[str, Any]:
        """Attach a review only when the selected key lacks exact trust."""
        result = dict(preview)
        policy = read_host_policy_snapshot(trust_store_path, allow_missing=True)
        publishers = policy.get("publishers", {})
        if preview["publisher_id"] not in publishers:
            return result  # First publisher admission retains its existing ceremony.
        if is_legacy_publisher_key(
            policy,
            publisher_id=str(preview["publisher_id"]),
            key_id=str(preview["key_id"]),
        ):
            return result  # The existing review binds any exact namespace addition.
        try:
            resolve_publisher_key(
                policy,
                publisher_id=str(preview["publisher_id"]),
                key_id=str(preview["key_id"]),
                pack_id=str(preview["pack_id"]),
                artifact_digest=str(preview["artifact_digest"]),
                signed_manifest_digest=signed_manifest_digest,
            )
        except ValueError:
            pass
        else:
            return result
        predecessor = preview.get("predecessor_artifact_digest")
        if not isinstance(predecessor, str) or not predecessor:
            raise ValueError("Native key addition requires a retained predecessor")
        context = _context(context_loader)
        approval_context = {
            **context,
            "preview_digest": preview["preview_digest"],
            "predecessor_artifact_digest": predecessor,
            "compatibility_digest": compatibility_digest,
        }
        plan = prepare_publisher_key_version(
            policy,
            publisher_id=str(preview["publisher_id"]),
            public_key_pem=public_key_pem,
            pack_id=str(preview["pack_id"]),
            artifact_digest=str(preview["artifact_digest"]),
            signed_manifest_digest=signed_manifest_digest,
            approval_context=approval_context,
        )
        document = plan.document()
        if (
            document["key_id"] != preview["key_id"]
            or document["new_key_fingerprint"] != preview["key_fingerprint"]
        ):
            raise ValueError("Native publisher key differs from the verified preview")
        issued = self._clock_ms()
        challenge, request_id = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
        section = {
            "schema": SCHEMA,
            "publisher_id": preview["publisher_id"],
            "pack_id": preview["pack_id"],
            "version": preview["version"],
            "artifact_digest": preview["artifact_digest"],
            "signed_manifest_digest": signed_manifest_digest,
            "previous_key_fingerprint": document["old_key_fingerprint"],
            "new_key_fingerprint": document["new_key_fingerprint"],
            "new_key_id": document["key_id"],
            "predecessor_artifact_digest": predecessor,
            "compatibility_digest": compatibility_digest,
            "policy_generation": document["policy_generation"],
            "policy_digest": document["policy_digest"],
            "publisher_keyset_digest": canonical_digest(
                {
                    "legacy": policy.get("publishers"),
                    "versions": policy.get("publisher_key_versions"),
                }
            ),
            **context,
            "issued_at_ms": issued,
            "expires_at_ms": issued + 300_000,
            "challenge": challenge,
        }
        section["authorization_digest"] = canonical_digest(section)
        with self._lock:
            self._pending = {
                key: value
                for key, value in self._pending.items()
                if issued < value.expires_at_ms
            }
            if len(self._pending) >= 64:
                raise ValueError("Native publisher reviews are busy")
            self._pending[challenge] = _PendingReview(
                plan,
                canonical_bytes(preview),
                canonical_bytes(section),
                request_id,
                issued,
                issued + 300_000,
            )
        result["key_version_authorization"] = section
        return result

    def discard(self, challenge: str) -> None:
        """Retire a known Native confirmation that failed before authorization."""
        with self._lock:
            self._pending.pop(challenge, None)

    def authorize(
        self,
        preview: Mapping[str, Any],
        *,
        challenge: str,
        authorization_digest: str,
        trust_store_path: Path,
        context_loader: ContextLoader,
    ) -> tuple[int, str]:
        """Consume one exact Native review, recapture authority, then append trust."""
        if not isinstance(challenge, str):
            raise ValueError("Native publisher confirmation is required")
        with self._lock:
            pending = self._pending.pop(challenge, None)
        if pending is None:
            raise ValueError("Native publisher review is missing or already consumed")
        if not isinstance(authorization_digest, str):
            raise ValueError("Native publisher confirmation is required")
        import json

        section = json.loads(pending.section_bytes)
        if (
            not hmac.compare_digest(
                section["authorization_digest"], authorization_digest
            )
            or canonical_bytes(preview) != pending.preview_bytes
        ):
            raise ValueError("Native publisher review changed after confirmation")
        expected_context = {
            key: pending.plan.document()["approval_context"][key]
            for key in _CONTEXT_FIELDS
        }
        proof = {
            "domain": DOMAIN,
            "request_id": pending.request_id,
            "issued_at_ms": pending.issued_at_ms,
            "expires_at_ms": pending.expires_at_ms,
            "plan_digest": pending.plan.digest,
            "context": pending.plan.document()["approval_context"],
            "evidence": {
                "native_challenge": challenge,
                "authorization_digest": authorization_digest,
            },
        }

        def verify(plan: PublisherKeyVersionPlan, supplied: Mapping[str, Any]) -> None:
            if (
                plan != pending.plan
                or canonical_bytes(supplied) != canonical_bytes(proof)
                or _context(context_loader) != expected_context
            ):
                raise ValueError(
                    "Native publisher authority changed after confirmation"
                )

        return write_publisher_key_version(
            trust_store_path,
            plan=pending.plan,
            approval_proof=proof,
            approval_verifier=verify,
            clock_ms=self._clock_ms,
        )
