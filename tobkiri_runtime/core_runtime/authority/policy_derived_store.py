"""Transactional guards for the isolated policy-derived grant candidate."""

from __future__ import annotations

import hmac
import secrets
from typing import Any, Protocol, cast
from collections.abc import Callable
from contextlib import AbstractContextManager

from core_runtime.authority.v4_models import AuthorityDenied, GrantRecord, authority_digest
from core_runtime.authority.store_contracts import (
    AuthorityStoreError,
    PendingEffectUpdate,
    _process_owned,
)

PREFIX = "policy-derived-v1-"
SELECTION_PREFIX = "action-approval-policy-selection-v1-"


class _PolicyStorePort(Protocol):
    """Private durable-store operations required by transactional guards."""

    _clock: Callable[[], float]
    _lock: AbstractContextManager[Any]

    def _decrypt(self, payload: bytes) -> dict[str, Any]: ...
    def _validated_pending_effect_payload(self, identifier: str, payload: Any) -> Any: ...
    def _is_revoked(self, connection: Any, kind: str, identifier: str) -> bool: ...
    def _connection(self) -> AbstractContextManager[Any]: ...
    def _apply_pending_effect_update(self, connection: Any, update: PendingEffectUpdate) -> Any: ...
    def _insert_record(self, connection: Any, record: GrantRecord) -> Any: ...
    def _append_audit(self, connection: Any, **kwargs: Any) -> Any: ...


class PolicyDerivedStoreMixin:
    """Check durable native root provenance in the same effect transaction."""

    def _policy_payload(self, connection: Any, identifier: str) -> tuple[int, dict[str, Any]]:
        row = connection.execute(
            "SELECT revision,payload_digest,encrypted_payload FROM host_pending_effects WHERE effect_id=?",
            (identifier,),
        ).fetchone()
        if row is None:
            raise AuthorityDenied("policy root receipt unavailable")
        payload = cast(_PolicyStorePort, self)._decrypt(row["encrypted_payload"])
        cast(_PolicyStorePort, self)._validated_pending_effect_payload(identifier, payload)
        if not hmac.compare_digest(str(row["payload_digest"]), authority_digest(payload)):
            raise AuthorityStoreError("policy root receipt authentication failed")
        return int(row["revision"]), payload

    def _check_policy_derived_guard(
        self, connection: Any, grant_id: str, *, settling_effect: bool = False
    ) -> dict[str, Any] | None:
        if not grant_id.startswith(PREFIX):
            return None
        pieces = grant_id.removeprefix(PREFIX).split("-")
        if len(pieces) != 2 or any(
            len(piece) != 64 or any(c not in "0123456789abcdef" for c in piece) for piece in pieces
        ):
            raise AuthorityDenied("policy grant locator malformed")
        _, payload = self._policy_payload(connection, SELECTION_PREFIX + pieces[0])
        entry = payload.get("policy_derived_grants", {}).get(grant_id)
        if (
            not entry
            or entry.get("authorization_kind") != "native_selected_policy_exact_v1"
            or entry.get("state") != "settled"
        ):
            raise AuthorityDenied("policy grant provenance unavailable")
        _, receipt = self._policy_payload(connection, entry["root_receipt_id"])
        if (
            receipt.get("state") != "committed_selection"
            or authority_digest(receipt) != entry["root_receipt_digest"]
            or receipt.get("root_native_request") != entry["root_native_request"]
            or cast(_PolicyStorePort, self)._clock() >= entry["root_expires_at"]
        ):
            raise AuthorityDenied("policy root was cancelled, replaced or expired")
        epoch = connection.execute(
            "SELECT value FROM authority_meta WHERE key='security_epoch'"
        ).fetchone()
        if epoch is None or int(epoch["value"]) != receipt["capture"]["context"]["security_epoch"]:
            raise AuthorityDenied("policy root SecurityEpoch changed in transaction")
        for kind, identifier in entry["root_revocation_targets"]:
            if cast(_PolicyStorePort, self)._is_revoked(connection, kind, identifier):
                raise AuthorityDenied("policy root authority revoked")
        parent_id = entry.get("parent_lease_id")
        if parent_id:
            row = connection.execute(
                "SELECT state,lease_digest,encrypted_payload FROM invocation_leases WHERE lease_id=?",
                (parent_id,),
            ).fetchone()
            if row is None or row["state"] != "dispatched":
                raise AuthorityDenied("policy parent no longer dispatched")
            parent = cast(_PolicyStorePort, self)._decrypt(row["encrypted_payload"])
            if not hmac.compare_digest(str(row["lease_digest"]), authority_digest(parent)):
                raise AuthorityStoreError("policy parent authentication failed")
            if parent["expires_at"] <= cast(_PolicyStorePort, self)._clock():
                raise AuthorityDenied("policy parent expired")
            for kind, identifier in (
                ("grant", parent["grant_id"]),
                ("provider_authority", parent["provider_authority_id"]),
                ("host_extension", parent["host_extension_id"]),
                ("function_principal", authority_digest(parent["caller"])),
                ("function_principal", authority_digest(parent["target"])),
                ("execution_domain", parent["caller_domain_id"]),
                ("execution_domain", parent["target_domain_id"]),
            ):
                if cast(_PolicyStorePort, self)._is_revoked(connection, kind, identifier):
                    raise AuthorityDenied("policy parent revoked in transaction")
        reviewer_lease_id = entry.get("review_transport_lease_id")
        if reviewer_lease_id:
            row = connection.execute(
                "SELECT state,lease_digest,encrypted_payload FROM invocation_leases WHERE lease_id=?",
                (reviewer_lease_id,),
            ).fetchone()
            if row is None or row["state"] != "committed":
                raise AuthorityDenied("review transport is not committed")
            review = cast(_PolicyStorePort, self)._decrypt(row["encrypted_payload"])
            if not hmac.compare_digest(str(row["lease_digest"]), authority_digest(review)):
                raise AuthorityStoreError("review transport authentication failed")
            if (
                review["expires_at"] <= cast(_PolicyStorePort, self)._clock()
                or entry.get("review_evidence_expires_at", 0)
                <= cast(_PolicyStorePort, self)._clock()
            ):
                raise AuthorityDenied("review transport or evidence expired in transaction")
            if review["request_digest"] != entry["review_transport_request_digest"]:
                raise AuthorityDenied("review transport exact digest changed")
            for kind, identifier in (
                ("grant", review["grant_id"]),
                ("provider_authority", review["provider_authority_id"]),
                ("host_extension", review["host_extension_id"]),
                ("function_principal", authority_digest(review["caller"])),
                ("function_principal", authority_digest(review["target"])),
                ("execution_domain", review["caller_domain_id"]),
                ("execution_domain", review["target_domain_id"]),
            ):
                if cast(_PolicyStorePort, self)._is_revoked(connection, kind, identifier):
                    raise AuthorityDenied("review transport authority revoked in transaction")
        effect_id = entry.get("pending_effect_id")
        if effect_id:
            _, effect = self._policy_payload(connection, effect_id)
            allowed = {"approved"} if settling_effect else {"claimed", "dispatched", "succeeded"}
            if (
                effect.get("expires_at", 0) <= cast(_PolicyStorePort, self)._clock()
                or effect.get("state") not in allowed
                or effect.get("authorization_kind") != "native_selected_policy_exact_v1"
                or effect.get("policy_plan", {}).get("selection_id") != SELECTION_PREFIX + pieces[0]
                or effect.get("prepared", {}).get("request_digest")
                != entry["retained_operation"]["context"]["request_digest"]
                or (not settling_effect and effect.get("policy_derived_grant_id") != grant_id)
            ):
                raise AuthorityDenied("policy effect was cancelled, replaced or not claimed")
        return entry

    @_process_owned
    def commit_policy_derived_grant(
        self,
        *,
        grant: GrantRecord,
        selection_id: str,
        expected_revision: int,
        payload: dict[str, Any],
    ) -> None:
        """Atomically consume review, authenticate live root, grant and audit."""
        with (
            cast(_PolicyStorePort, self)._lock,
            cast(_PolicyStorePort, self)._connection() as connection,
        ):
            connection.execute("BEGIN IMMEDIATE")
            cast(_PolicyStorePort, self)._apply_pending_effect_update(
                connection,
                PendingEffectUpdate(
                    effect_id=selection_id, expected_revision=expected_revision, payload=payload
                ),
            )
            entry = self._check_policy_derived_guard(
                connection, grant.grant_id, settling_effect=True
            )
            if entry is None or entry["grant_digest"] != grant.digest:
                raise AuthorityDenied("policy derived grant digest changed")
            cast(_PolicyStorePort, self)._insert_record(connection, grant)
            cast(_PolicyStorePort, self)._append_audit(
                connection,
                event_id="policy-derived-" + secrets.token_hex(16),
                event_type="policy_derived_authority",
                event_state="settled",
                payload={
                    "authorization_kind": entry["authorization_kind"],
                    "grant_id": grant.grant_id,
                    "grant_digest": grant.digest,
                    "root_native_request": entry["root_native_request"],
                    "operation_capture_digest": entry["operation_capture_digest"],
                    "review_evidence_id": entry["review_evidence_id"],
                },
            )
            connection.commit()
