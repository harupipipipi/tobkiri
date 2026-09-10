"""Host-owned clipboard transport for an already dispatched canonical lease."""

from __future__ import annotations

import threading
import time

from core_runtime.authority.v4 import AuthorityStore, LeaseState
from tobkiri_host.broker import RequestEnvelope
from tobkiri_host.clipboard_native import MacOSTextClipboard
from tobkiri_host.effects import ProviderOutcome
from tobkiri_protocol.canonical import canonical_digest

_OPERATIONS = {
    ("tobkiri.resource.clipboard.v1", "rumi_clipboard_host_service_pack.clipboard-read"): "read",
    ("tobkiri.action.clipboard.v1", "rumi_clipboard_host_service_pack.clipboard-write"): "write",
}


class ClipboardTransportV4:
    """Revalidate Authority state and deliver one exact finite native operation.

    This object stays in the Host. It neither issues Grants nor stores authority;
    its in-memory claim set only rejects duplicate entry during a live lease.
    Restarted/completed/expired leases remain rejected by the existing store.
    """

    def __init__(self, store: AuthorityStore) -> None:
        self._store = store
        self._native = MacOSTextClipboard()
        self._claims: dict[str, float] = {}
        self._lock = threading.RLock()

    def invoke(self, envelope: RequestEnvelope) -> ProviderOutcome:
        """Use only the Broker's payload, target and deadline, never client IDs."""
        access = _OPERATIONS.get((envelope.contract_id, envelope.operation_id))
        if access is None or envelope.contract_version != "1.0.0":
            raise PermissionError("clipboard contract binding is invalid")
        expected_digest = canonical_digest({
            "request_id": envelope.context.request_id,
            "profile_revision": envelope.context.profile_revision,
            "activation_digest": envelope.context.activation_digest,
            "plan_digest": envelope.context.plan_digest,
            "target": envelope.target_principal.value,
            "contract_id": envelope.contract_id,
            "contract_version": envelope.contract_version,
            "operation_id": envelope.operation_id,
            "payload": envelope.payload,
            "idempotency_key": envelope.idempotency_key,
        })
        if expected_digest != envelope.request_digest:
            raise PermissionError("clipboard payload binding changed")
        try:
            token = envelope.lease.token.decode("ascii")
            lease, _ = self._store.inspect_lease_token(token)
        except Exception as exc:
            raise PermissionError("clipboard lease is unavailable") from exc

        def check() -> None:
            self._check(envelope, token)

        check()
        with self._lock:
            now = time.time()
            self._claims = {key: expiry for key, expiry in self._claims.items() if expiry > now}
            if lease.lease_id in self._claims:
                raise PermissionError("clipboard invocation replay")
            self._claims[lease.lease_id] = lease.expires_at
        return self._native.execute(
            access, envelope.payload,
            deadline=envelope.deadline_monotonic, check_authority=check,
        )

    def _check(self, envelope: RequestEnvelope, token: str) -> None:
        try:
            lease, state = self._store.inspect_lease_token(token)
            context = envelope.context
            if (
                envelope.cancellation_requested.is_set()
                or state is not LeaseState.DISPATCHED
                or time.time() >= lease.expires_at
                or time.monotonic() >= envelope.deadline_monotonic
                or lease.security_epoch != self._store.security_epoch
                or lease.caller.principal_id != context.caller_principal.value
                or lease.target.principal_id != envelope.target_principal.value
                or lease.target.operation_id != envelope.operation_id
                or envelope.target_domain.value != context.target_domain_id
                or lease.request_digest != envelope.request_digest
            ):
                raise PermissionError("clipboard invocation is inactive")
            for field in (
                "request_id", "profile_id", "activation_id", "activation_digest",
                "plan_digest", "profile_authority_digest", "security_epoch",
                "caller_domain_id", "caller_boot_epoch", "target_domain_id",
                "target_boot_epoch", "fencing_token",
            ):
                if getattr(lease, field) != getattr(context, field):
                    raise PermissionError("clipboard invocation binding changed")
            targets = (
                ("function_principal", lease.caller.principal_id),
                ("function_principal", lease.target.principal_id),
                ("execution_domain", lease.caller_domain_id),
                ("execution_domain", lease.target_domain_id),
                ("profile", lease.profile_id), ("activation", lease.activation_id),
                ("grant", lease.grant_id),
                ("provider_authority", lease.provider_authority_id),
                ("pack_artifact", lease.caller.parent_artifact_digest),
                ("pack_artifact", lease.target.parent_artifact_digest),
                ("publisher", lease.caller_publisher_lineage),
                ("publisher", lease.target_publisher_lineage),
                ("host_extension", lease.host_extension_id),
            )
            if any(self._store.is_revoked(kind, identity) for kind, identity in targets):
                raise PermissionError("clipboard authority was revoked")
            now = time.time()
            provider = self._store.get_provider_authority(lease.provider_authority_id)
            grant = self._store.get_grant(lease.grant_id)
            if provider is None or grant is None:
                raise PermissionError("clipboard authority record is unavailable")
            if provider.valid_from > now or grant.issued_at > now:
                raise PermissionError("clipboard authority record is not active")
            for record in (provider, grant):
                if (
                    record.expires_at is not None and record.expires_at <= now
                ):
                    raise PermissionError("clipboard authority record expired")
            trust = self._store.get_host_extension_trust(lease.host_extension_id)
            if (
                trust is None
                or trust.trust_id != lease.host_extension_id
                or trust.revoked
                or trust.security_epoch != lease.security_epoch
                or trust.package_kind != "host_extension"
                or trust.parent_artifact_digest != lease.target.parent_artifact_digest
                or trust.publisher_lineage != lease.target_publisher_lineage
                or lease.target.principal_id not in trust.provider_principal_ids
                or trust.valid_from > now
                or (trust.expires_at is not None and trust.expires_at <= now)
            ):
                raise PermissionError("clipboard Host Extension trust is unavailable")
            for domain_id, boot_epoch, principal in (
                (lease.caller_domain_id, lease.caller_boot_epoch, lease.caller),
                (lease.target_domain_id, lease.target_boot_epoch, lease.target),
            ):
                domain = self._store.get_domain(domain_id)
                if (
                    domain is None or domain.state.value != "active"
                    or domain.boot_epoch != boot_epoch
                    or domain.security_epoch != lease.security_epoch
                    or principal.principal_id not in domain.principal_ids
                ):
                    raise PermissionError("clipboard execution domain changed")
        except PermissionError:
            raise
        except Exception as exc:
            raise PermissionError("clipboard authority is unavailable") from exc


__all__ = ["ClipboardTransportV4"]
