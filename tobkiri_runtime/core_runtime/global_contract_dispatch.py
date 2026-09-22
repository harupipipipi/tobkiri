"""Finite adapter from typed consumers to one captured Pack v4 session."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol


class GlobalContractUnavailable(RuntimeError):
    """Raised when an operation is absent from the captured ResolvedPlan."""


class GlobalContractInvocationError(RuntimeError):
    """Preserve a provider-neutral operation failure code across isolation."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class HostCredentialTransportError(RuntimeError):
    """Fixed failure raised when a Host credential capability cannot complete."""

    code = "host_credential_transport_failed"

    def __init__(self) -> None:
        super().__init__(self.code)


class V4ContractDispatch(Protocol):
    """Explicit Host adapter; implementations own an immutable activation."""

    profile_id: str
    plan_digest: str

    def invoke(
        self,
        contract_id: str,
        operation_id: str,
        payload: Mapping[str, Any],
        *,
        version_range: str | None = None,
    ) -> Mapping[str, Any]:
        """Invoke one exact ResolvedPlan route."""

    def provider_metadata(self, contract_id: str) -> tuple[Mapping[str, Any], ...]:
        """Return non-executable metadata captured with the same activation."""


class HostCredentialTransport(Protocol):
    """Request-scoped Host capability that applies a credential at transport."""

    def post_json(
        self,
        *,
        endpoint: str,
        headers: Mapping[str, str],
        body: Mapping[str, Any],
        credential_handle: str,
        provider_instance_id: str,
        credential_scope: str,
        credential_scheme: str,
        deadline: float,
    ) -> Mapping[str, Any]:
        """Perform exactly one Host-bound credentialed JSON request."""


def _require_v4_session(value: object) -> V4ContractDispatch:
    if not callable(getattr(value, "invoke", None)) or not callable(
        getattr(value, "provider_metadata", None)
    ):
        raise GlobalContractUnavailable(
            "Pack v4 dispatch session is required; live registry lookup is disabled"
        )
    return value  # type: ignore[return-value]


def invoke_global_contract(
    session: V4ContractDispatch,
    contract_id: str,
    operation: str,
    payload: Mapping[str, Any],
) -> Any:
    """Invoke only the operation pinned by the captured Pack v4 session."""
    return _require_v4_session(session).invoke(contract_id, operation, payload)


def selected_global_providers(
    session: V4ContractDispatch,
    contract_id: str,
) -> tuple[dict[str, Any], ...]:
    """Return immutable provider metadata from the captured activation."""
    return tuple(dict(item) for item in _require_v4_session(session).provider_metadata(contract_id))


def captured_profile_id(session: V4ContractDispatch) -> str:
    """Return the profile identity pinned to this exact activation snapshot."""
    value = str(getattr(_require_v4_session(session), "profile_id", "")).strip()
    if not value:
        raise GlobalContractUnavailable("Pack v4 dispatch session has no captured profile identity")
    return value


def invoke_selected_global_provider(
    session: V4ContractDispatch,
    contract_id: str,
    provider_instance_id: str,
    operation: str,
    payload: Mapping[str, Any],
) -> Any:
    """Invoke after exact provider identity confirmation from snapshot metadata."""
    providers = selected_global_providers(session, contract_id)
    matches = [
        item for item in providers if item.get("provider_instance_id") == provider_instance_id
    ]
    if len(matches) != 1:
        raise GlobalContractUnavailable(
            f"selected provider is unavailable: {contract_id}/{provider_instance_id}"
        )
    return _require_v4_session(session).invoke(contract_id, operation, payload)


@dataclass(frozen=True)
class GlobalContractClient:
    """Restricted typed consumer over one explicit Pack v4 session."""

    session: V4ContractDispatch
    allowed_contract_ids: frozenset[str]
    consumer_pack_id: str
    host_credential_transport: HostCredentialTransport | None = None

    def providers(self, contract_id: str) -> tuple[dict[str, Any], ...]:
        """List selected metadata only for a manifest-declared requirement."""
        self._require_declared(contract_id)
        return selected_global_providers(self.session, contract_id)

    def invoke(
        self,
        contract_id: str,
        operation: str,
        payload: Mapping[str, Any],
        *,
        provider_instance_id: str | None = None,
    ) -> Any:
        """Invoke one declared requirement without registry or profile fallback."""
        self._require_declared(contract_id)
        if provider_instance_id is None:
            return invoke_global_contract(self.session, contract_id, operation, payload)
        return invoke_selected_global_provider(
            self.session,
            contract_id,
            provider_instance_id,
            operation,
            payload,
        )

    def post_json_with_credential(
        self,
        *,
        endpoint: str,
        headers: Mapping[str, str],
        body: Mapping[str, Any],
        credential_handle: str,
        provider_instance_id: str,
        credential_scope: str,
        credential_scheme: str,
        deadline: float,
    ) -> dict[str, Any]:
        """Use the finite Host transport capability; never resolve material."""
        if self.host_credential_transport is None:
            raise PermissionError("Host credential transport is unavailable")
        try:
            value = self.host_credential_transport.post_json(
                endpoint=endpoint,
                headers=headers,
                body=body,
                credential_handle=credential_handle,
                provider_instance_id=provider_instance_id,
                credential_scope=credential_scope,
                credential_scheme=credential_scheme,
                deadline=deadline,
            )
            if not isinstance(value, Mapping):
                raise HostCredentialTransportError
            return dict(value)
        except Exception:
            pass
        raise HostCredentialTransportError

    def _require_declared(self, contract_id: str) -> None:
        if contract_id not in self.allowed_contract_ids:
            raise PermissionError(f"contract was not declared by consumer: {contract_id}")


__all__ = [
    "GlobalContractClient",
    "GlobalContractInvocationError",
    "GlobalContractUnavailable",
    "HostCredentialTransportError",
    "V4ContractDispatch",
    "captured_profile_id",
    "invoke_global_contract",
    "invoke_selected_global_provider",
    "selected_global_providers",
]
