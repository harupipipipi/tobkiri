"""Finite Host Extension SDK registration owned by the Host trust boundary."""

from __future__ import annotations

import json
import math
import sqlite3
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from threading import RLock
from typing import Any, Protocol

from core_runtime.authority.v4 import (
    AuthorityDenied,
    AuthorityMode,
    AuthorityScope,
    ExecutionDomain,
    FunctionPrincipal,
    HostExtensionTrustRecord,
    ProviderAuthorityRecord,
)
from tobkiri_protocol.canonical import canonical_digest

from .errors import AuthorizationError
from .models import PackageKind, PackArtifact


class AuthorityRegistrationStore(Protocol):
    """Narrow durable authority-store surface used by Host registration."""

    @property
    def security_epoch(self) -> int:
        """Return the current Host-owned SecurityEpoch."""

    def commit_host_extension_registration(
        self, records: Iterable[Any], state: Mapping[str, Any], event: Mapping[str, Any]
    ) -> None:
        """Commit records, lifecycle state and audit in one transaction."""

    def host_extension_registration(
        self, registration_id: str
    ) -> Mapping[str, Any] | None:
        """Read durable registration state."""

    def host_extension_audit_events(
        self, registration_id: str
    ) -> Iterable[Mapping[str, Any]]:
        """Read authenticated SDK events."""

    def finish_host_extension_revocation(
        self, registration_id: str, event: Mapping[str, Any]
    ) -> None:
        """Commit inactive lifecycle state after Kernel revocation."""

    def append_host_extension_event(self, event: Mapping[str, Any]) -> None:
        """Append an authoritative lifecycle event."""

    def migrate_host_extension_registrations(
        self, states: Iterable[Mapping[str, Any]], events: Iterable[Mapping[str, Any]]
    ) -> None:
        """Validate and idempotently import legacy lifecycle data."""


class AuthorityRegistrationKernel(Protocol):
    """Narrow Host-owned mutation boundary used by the SDK."""

    @property
    def store(self) -> AuthorityRegistrationStore:
        """Return the durable authority store."""

    def revoke(self, *, target_kind: str, target_id: str, reason: str) -> str:
        """Durably revoke an exact authority target."""


@dataclass(frozen=True)
class CapabilityProviderRegistration:
    """Complete registration for one exact Provider Function/Operation."""

    provider_id: str
    function_id: str
    contract_id: str
    operation_id: str
    capability: str
    scope_semantics_digest: str
    provider_ceiling: AuthorityScope
    authority_mode: AuthorityMode
    execution_domain: ExecutionDomain
    input_schema: Mapping[str, Any]
    output_schema: Mapping[str, Any]
    error_schema: Mapping[str, Any] | None
    progress_schema: Mapping[str, Any] | None
    attenuation_definition: Mapping[str, Any]
    approval_metadata: Mapping[str, Any]
    audit_metadata: Mapping[str, Any]
    conformance_vectors: tuple[Mapping[str, Any], ...]
    host_broker_binding: str | None = None
    os_entitlements: tuple[str, ...] = ()
    background_allowed: bool = False
    network_allowed: bool = False
    process_allowed: bool = False
    expires_at: float | None = None


@dataclass(frozen=True)
class HostExtensionRegistration:
    """Signed Host Extension artifact and its finite declared Providers."""

    registration_id: str
    host_extension_id: str
    trust_id: str
    artifact: PackArtifact
    trust_provenance_digest: str
    providers: tuple[CapabilityProviderRegistration, ...]
    valid_from: float
    expires_at: float | None = None


class HostExtensionSDK:
    """Register, revoke, update, and audit exact Host Extension Providers.

    This surface accepts verified ``PackArtifact`` objects, never Profile or Pack
    manifest dictionaries.  It cannot install evaluator, matcher, renderer, or
    identity-resolver code into the authority kernel. The supplied audit database
    is only a read-only migration source; new state and audit live with authority.
    Revocation retains the Kernel's durable revoke and domain-stop sequence. It
    is fail-closed and retryable, not an atomic group revoke across SDK instances.
    """

    def __init__(
        self,
        authority: AuthorityRegistrationKernel,
        audit_database: sqlite3.Connection,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._authority = authority
        self._store: AuthorityRegistrationStore = authority.store
        self._clock = clock
        self._lock = RLock()
        required = (
            "commit_host_extension_registration",
            "host_extension_registration",
            "host_extension_audit_events",
            "finish_host_extension_revocation",
            "append_host_extension_event",
            "migrate_host_extension_registrations",
        )
        if any(not callable(getattr(self._store, name, None)) for name in required):
            raise AuthorizationError(
                "authority store lacks atomic Host registration support"
            )
        self._migrate_legacy(audit_database)

    def _migrate_legacy(self, source: sqlite3.Connection) -> None:
        """Read a consistent legacy snapshot without changing its contents."""
        source.execute("SAVEPOINT host_extension_import")
        try:
            self._read_legacy(source)
        finally:
            source.execute("RELEASE SAVEPOINT host_extension_import")

    def _read_legacy(self, source: sqlite3.Connection) -> None:
        """Read the old SDK database without modifying or deleting its history."""
        tables = {
            row[0]
            for row in source.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        expected = {
            "host_extension_registration_state",
            "host_extension_registration_audit",
        }
        if not tables.intersection(expected):
            return
        if not expected.issubset(tables):
            raise AuthorizationError("legacy Host Extension state is incomplete")
        states = []
        for row in source.execute(
            "SELECT registration_id, trust_id, artifact_digest, provider_record_ids, active "
            "FROM host_extension_registration_state ORDER BY registration_id"
        ):
            ids = json.loads(row[3])
            if (
                not isinstance(ids, list)
                or not ids
                or len(set(ids)) != len(ids)
                or any(not isinstance(item, str) or not item for item in ids)
                or row[4] not in (0, 1)
            ):
                raise AuthorizationError("legacy Host Extension state is invalid")
            states.append(
                {
                    "registration_id": row[0],
                    "trust_id": row[1],
                    "artifact_digest": row[2],
                    "provider_record_ids": ids,
                    "active": bool(row[4]),
                }
            )
        events = []
        by_id = {state["registration_id"]: state for state in states}
        for row in source.execute(
            "SELECT sequence, registration_id, event_type, artifact_digest, "
            "provider_record_ids, security_epoch, event_time "
            "FROM host_extension_registration_audit ORDER BY sequence"
        ):
            ids = json.loads(row[4])
            state = by_id.get(row[1])
            if (
                row[0] <= 0
                or row[5] <= 0
                or not math.isfinite(row[6])
                or not isinstance(ids, list)
                or row[2] not in {"registered", "revoked", "update_failed_closed"}
                or (
                    row[2] != "update_failed_closed"
                    and (
                        state is None
                        or ids != state["provider_record_ids"]
                        or row[3] != state["artifact_digest"]
                    )
                )
                or (row[2] == "update_failed_closed" and ids)
            ):
                raise AuthorizationError("legacy Host Extension audit is invalid")
            events.append(
                {
                    "sequence": row[0],
                    "registration_id": row[1],
                    "event_type": row[2],
                    "artifact_digest": row[3],
                    "provider_record_ids": ids,
                    "security_epoch": row[5],
                    "event_time": row[6],
                }
            )
        for state in states:
            history = [
                event
                for event in events
                if event["registration_id"] == state["registration_id"]
            ]
            if not history or history[0]["event_type"] != "registered":
                raise AuthorizationError(
                    "legacy Host Extension registration lacks audit history"
                )
            if any(event["event_type"] == "revoked" for event in history):
                state["active"] = False
        self._store.migrate_host_extension_registrations(states, events)

    def _active_registration(
        self, registration_id: str, *, include_pending: bool = False
    ) -> tuple[str, tuple[str, ...], str] | None:
        """Read the durable lifecycle state, including after an SDK restart."""
        state = self._store.host_extension_registration(registration_id)
        if state is None or (
            not state["active"]
            and not (include_pending and state.get("revocation_pending", False))
        ):
            return None
        return (
            state["trust_id"],
            tuple(state["provider_record_ids"]),
            state["artifact_digest"],
        )

    def register(self, request: HostExtensionRegistration) -> tuple[str, ...]:
        """Atomically register exact Provider identities or fail closed."""
        trust, domains, authorities = self._compile(request)
        records: tuple[
            HostExtensionTrustRecord | ExecutionDomain | ProviderAuthorityRecord,
            ...,
        ] = (trust, *domains, *authorities)
        with self._lock:
            record_ids = tuple(item.record_id for item in authorities)
            state = {
                "registration_id": request.registration_id,
                "trust_id": request.trust_id,
                "artifact_digest": request.artifact.digest,
                "provider_record_ids": list(record_ids),
                "active": True,
            }
            try:
                self._store.commit_host_extension_registration(
                    records,
                    state,
                    self._event(
                        request.registration_id,
                        "registered",
                        request.artifact.digest,
                        record_ids,
                    ),
                )
            except AuthorityDenied as exc:
                raise AuthorizationError(str(exc)) from exc
            return record_ids

    def revoke(self, registration_id: str, *, reason: str) -> None:
        """Durably revoke one registration and all its Provider authorities."""
        with self._lock:
            active = self._active_registration(registration_id, include_pending=True)
            if active is None:
                raise AuthorizationError("Host Extension registration is not active")
            trust_id, record_ids, artifact_digest = active
            for record_id in record_ids:
                self._authority.revoke(
                    target_kind="provider_authority",
                    target_id=record_id,
                    reason=reason,
                )
            self._authority.revoke(
                target_kind="host_extension",
                target_id=trust_id,
                reason=reason,
            )
            try:
                self._store.finish_host_extension_revocation(
                    registration_id,
                    self._event(
                        registration_id, "revoked", artifact_digest, record_ids
                    ),
                )
            except AuthorityDenied as exc:
                raise AuthorizationError(str(exc)) from exc

    def update(
        self,
        previous_registration_id: str,
        successor: HostExtensionRegistration,
        *,
        reason: str,
    ) -> tuple[str, ...]:
        """Revoke the old exact artifact before registering its successor."""
        with self._lock:
            active = self._active_registration(previous_registration_id)
            if active is None:
                raise AuthorizationError("Host Extension predecessor is not active")
            if active[2] == successor.artifact.digest:
                raise AuthorizationError(
                    "Host Extension update requires a new artifact"
                )
            self.revoke(previous_registration_id, reason=reason)
            try:
                result = self.register(successor)
            except Exception:
                self._audit(
                    successor.registration_id,
                    "update_failed_closed",
                    successor.artifact.digest,
                    (),
                )
                raise
            return result

    def audit_events(self, registration_id: str) -> tuple[Mapping[str, Any], ...]:
        """Return the finite ordered Host registration audit history."""
        return tuple(
            {
                key: (tuple(value) if key == "provider_record_ids" else value)
                for key, value in event.items()
                if key != "registration_id"
            }
            for event in self._store.host_extension_audit_events(registration_id)
        )

    def _compile(
        self,
        request: HostExtensionRegistration,
    ) -> tuple[
        HostExtensionTrustRecord,
        tuple[ExecutionDomain, ...],
        tuple[ProviderAuthorityRecord, ...],
    ]:
        artifact = request.artifact
        if artifact.package_kind is not PackageKind.HOST_EXTENSION:
            raise AuthorizationError(
                "normal Pack/Profile cannot register Host authority"
            )
        if request.host_extension_id != artifact.pack_id:
            raise AuthorizationError("Host Extension namespace does not match artifact")
        if not request.providers:
            raise AuthorizationError("Host Extension declares no Providers")
        epoch = self._store.security_epoch
        principal_ids: list[str] = []
        domains: list[ExecutionDomain] = []
        authorities: list[ProviderAuthorityRecord] = []
        seen_providers: set[str] = set()
        seen_domains: set[str] = set()
        for definition in request.providers:
            prefix = f"{request.host_extension_id}."
            if not definition.provider_id.startswith(prefix):
                raise AuthorizationError("Provider ID is outside extension namespace")
            if definition.provider_id in seen_providers:
                raise AuthorizationError("duplicate Provider registration")
            seen_providers.add(definition.provider_id)
            function = artifact.function(definition.function_id)
            operations = [
                item
                for item in function.operations
                if item.contract_id == definition.contract_id
                and item.operation_id == definition.operation_id
            ]
            if len(operations) != 1:
                raise AuthorizationError(
                    "Provider operation is outside artifact inventory"
                )
            operation = operations[0]
            if (
                canonical_digest(operation.input_schema)
                != canonical_digest(definition.input_schema)
                or canonical_digest(operation.output_schema)
                != canonical_digest(definition.output_schema)
                or canonical_digest(operation.error_schema or {})
                != canonical_digest(definition.error_schema or {})
                or canonical_digest(operation.progress_schema or {})
                != canonical_digest(definition.progress_schema or {})
            ):
                raise AuthorizationError(
                    "Provider schema does not match exact operation"
                )
            if (
                definition.scope_semantics_digest
                != definition.provider_ceiling.semantics_digest
            ):
                raise AuthorizationError("Provider scope semantics digest mismatch")
            if not definition.conformance_vectors:
                raise AuthorizationError("Provider conformance vectors are required")
            if not definition.approval_metadata or not definition.audit_metadata:
                raise AuthorizationError("Provider approval/audit metadata is required")
            if not definition.attenuation_definition:
                raise AuthorizationError("Provider attenuation definition is required")
            principal = FunctionPrincipal(
                parent_artifact_digest=artifact.digest,
                function_implementation_digest=function.implementation_digest,
                function_id=function.function_id,
                contract_revision_digest=operation.revision_digest,
                operation_id=operation.operation_id,
            )
            domain = definition.execution_domain
            if (
                domain.security_epoch != epoch
                or domain.principals != (principal,)
                or domain.domain_id in seen_domains
            ):
                raise AuthorizationError(
                    "Provider domain must isolate one exact principal at current epoch"
                )
            seen_domains.add(domain.domain_id)
            principal_ids.append(principal.principal_id)
            domains.append(domain)
            authorities.append(
                ProviderAuthorityRecord(
                    record_id=f"provider-authority.{request.registration_id}.{len(authorities)}",
                    provider=principal,
                    execution_domain_id=domain.domain_id,
                    execution_domain_identity_digest=domain.identity_digest,
                    scope=definition.provider_ceiling,
                    authority_mode=definition.authority_mode,
                    security_epoch=epoch,
                    trust_provenance_digest=request.trust_provenance_digest,
                    publisher_lineage=artifact.publisher_lineage,
                    host_extension_id=request.host_extension_id,
                    valid_from=request.valid_from,
                    expires_at=definition.expires_at,
                    os_entitlements=definition.os_entitlements,
                    host_broker_binding=definition.host_broker_binding,
                    background_allowed=definition.background_allowed,
                    network_allowed=definition.network_allowed,
                    process_allowed=definition.process_allowed,
                )
            )
        trust = HostExtensionTrustRecord(
            trust_id=request.trust_id,
            parent_artifact_digest=artifact.digest,
            publisher_lineage=artifact.publisher_lineage,
            provider_principal_ids=tuple(principal_ids),
            trust_provenance_digest=request.trust_provenance_digest,
            security_epoch=epoch,
            valid_from=request.valid_from,
            expires_at=request.expires_at,
        )
        return trust, tuple(domains), tuple(authorities)

    def _audit(
        self,
        registration_id: str,
        event_type: str,
        artifact_digest: str,
        record_ids: tuple[str, ...],
    ) -> None:
        self._store.append_host_extension_event(
            self._event(registration_id, event_type, artifact_digest, record_ids)
        )

    def _event(
        self,
        registration_id: str,
        event_type: str,
        artifact_digest: str,
        record_ids: tuple[str, ...],
    ) -> dict[str, Any]:
        return {
            "registration_id": registration_id,
            "event_type": event_type,
            "artifact_digest": artifact_digest,
            "provider_record_ids": list(record_ids),
            "security_epoch": self._store.security_epoch,
            "event_time": self._clock(),
        }


__all__ = [
    "CapabilityProviderRegistration",
    "HostExtensionRegistration",
    "HostExtensionSDK",
]
