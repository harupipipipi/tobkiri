"""Finite Host Extension SDK registration owned by the Host trust boundary."""

from __future__ import annotations

from dataclasses import dataclass
import json
import sqlite3
from threading import RLock
import time
from typing import Any, Callable, Iterable, Mapping, Protocol

from core_runtime.authority.v4 import (
    AuthorityMode,
    AuthorityScope,
    ApprovalRecord,
    ExecutionDomain,
    FunctionPrincipal,
    GrantRecord,
    HostExtensionTrustRecord,
    ProviderAuthorityRecord,
)
from tobkiri_protocol.canonical import canonical_digest

from .errors import AuthorizationError
from .models import PackArtifact, PackageKind


class AuthorityRegistrationStore(Protocol):
    """Narrow durable authority-store surface used by Host registration."""

    @property
    def security_epoch(self) -> int:
        """Return the current Host-owned SecurityEpoch."""

    def put_records_atomically(
        self,
        records: Iterable[
            ProviderAuthorityRecord
            | ApprovalRecord
            | GrantRecord
            | ExecutionDomain
            | HostExtensionTrustRecord
        ],
    ) -> None:
        """Commit an immutable trust/domain/provider set atomically."""


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
    identity-resolver code into the authority kernel.
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
        self._database = audit_database
        self._clock = clock
        self._lock = RLock()
        self._database.execute(
            """
            CREATE TABLE IF NOT EXISTS host_extension_registration_audit (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                registration_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                artifact_digest TEXT NOT NULL,
                provider_record_ids TEXT NOT NULL,
                security_epoch INTEGER NOT NULL,
                event_time REAL NOT NULL
            )
            """
        )
        self._database.execute(
            """
            CREATE TABLE IF NOT EXISTS host_extension_registration_state (
                registration_id TEXT PRIMARY KEY,
                trust_id TEXT NOT NULL,
                artifact_digest TEXT NOT NULL,
                provider_record_ids TEXT NOT NULL,
                active INTEGER NOT NULL
            )
            """
        )
        self._database.commit()
        self._active: dict[str, tuple[str, tuple[str, ...], str]] = {}

    def register(self, request: HostExtensionRegistration) -> tuple[str, ...]:
        """Atomically register exact Provider identities or fail closed."""
        trust, domains, authorities = self._compile(request)
        records: tuple[
            HostExtensionTrustRecord | ExecutionDomain | ProviderAuthorityRecord,
            ...,
        ] = (trust, *domains, *authorities)
        with self._lock:
            if request.registration_id in self._active:
                raise AuthorizationError("Host Extension registration already exists")
            self._store.put_records_atomically(records)
            record_ids = tuple(item.record_id for item in authorities)
            with self._database:
                self._database.execute(
                    """
                    INSERT INTO host_extension_registration_state (
                        registration_id, trust_id, artifact_digest,
                        provider_record_ids, active
                    ) VALUES (?, ?, ?, ?, 1)
                    """,
                    (
                        request.registration_id,
                        request.trust_id,
                        request.artifact.digest,
                        json.dumps(record_ids, separators=(",", ":")),
                    ),
                )
            self._active[request.registration_id] = (
                request.trust_id,
                record_ids,
                request.artifact.digest,
            )
            self._audit(
                request.registration_id,
                "registered",
                request.artifact.digest,
                record_ids,
            )
            return record_ids

    def revoke(self, registration_id: str, *, reason: str) -> None:
        """Durably revoke one registration and all its Provider authorities."""
        with self._lock:
            active = self._active.pop(registration_id, None)
            if active is None:
                row = self._database.execute(
                    """
                    SELECT trust_id, provider_record_ids, artifact_digest
                    FROM host_extension_registration_state
                    WHERE registration_id = ? AND active = 1
                    """,
                    (registration_id,),
                ).fetchone()
                if row is None:
                    raise AuthorizationError("Host Extension registration is not active")
                active = (
                    str(row[0]),
                    tuple(str(item) for item in json.loads(str(row[1]))),
                    str(row[2]),
                )
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
            with self._database:
                self._database.execute(
                    """
                    UPDATE host_extension_registration_state SET active = 0
                    WHERE registration_id = ?
                    """,
                    (registration_id,),
                )
            self._audit(registration_id, "revoked", artifact_digest, record_ids)

    def update(
        self,
        previous_registration_id: str,
        successor: HostExtensionRegistration,
        *,
        reason: str,
    ) -> tuple[str, ...]:
        """Revoke the old exact artifact before registering its successor."""
        active = self._active.get(previous_registration_id)
        if active is None:
            raise AuthorizationError("Host Extension predecessor is not active")
        if active[2] == successor.artifact.digest:
            raise AuthorizationError("Host Extension update requires a new artifact")
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
        rows = self._database.execute(
            """
            SELECT sequence, event_type, artifact_digest, provider_record_ids,
                   security_epoch, event_time
            FROM host_extension_registration_audit
            WHERE registration_id = ? ORDER BY sequence
            """,
            (registration_id,),
        ).fetchall()
        return tuple(
            {
                "sequence": int(row[0]),
                "event_type": str(row[1]),
                "artifact_digest": str(row[2]),
                "provider_record_ids": tuple(json.loads(str(row[3]))),
                "security_epoch": int(row[4]),
                "event_time": float(row[5]),
            }
            for row in rows
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
                raise AuthorizationError("Provider operation is outside artifact inventory")
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
                raise AuthorizationError("Provider schema does not match exact operation")
            if definition.scope_semantics_digest != definition.provider_ceiling.semantics_digest:
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
        with self._database:
            self._database.execute(
                """
                INSERT INTO host_extension_registration_audit (
                    registration_id, event_type, artifact_digest,
                    provider_record_ids, security_epoch, event_time
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    registration_id,
                    event_type,
                    artifact_digest,
                    json.dumps(record_ids, separators=(",", ":")),
                    self._store.security_epoch,
                    self._clock(),
                ),
            )


__all__ = [
    "CapabilityProviderRegistration",
    "HostExtensionRegistration",
    "HostExtensionSDK",
]
