"""Typed consumer clients over opaque provider handles."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

from .models import ContractResult
from .registry import ContractRegistry

T = TypeVar("T")


@dataclass(frozen=True)
class ServiceHandle(Generic[T]):
    """Opaque service handle that does not expose a pack or source path."""

    provider_instance_id: str
    contract_id: str
    contract_version: str
    _registry: ContractRegistry = field(repr=False, compare=False)
    expected_revision: str | None = None

    def __post_init__(self) -> None:
        """Reject empty handle identities before any provider invocation."""
        if not self.provider_instance_id:
            raise ValueError("provider_instance_id must not be empty")
        if not self.contract_id:
            raise ValueError("contract_id must not be empty")
        if not self.contract_version:
            raise ValueError("contract_version must not be empty")

    def call(
        self,
        operation: str,
        payload: Mapping[str, Any],
    ) -> ContractResult[T]:
        """Invoke a provider operation through the registry boundary."""
        return self._registry.invoke(
            self.provider_instance_id,
            operation,
            payload,
            contract_id=self.contract_id,
            contract_version=self.contract_version,
            expected_revision=self.expected_revision,
        )


class ActionClient(ServiceHandle[T]):
    """Typed client for action contracts."""


class EventClient(ServiceHandle[T]):
    """Typed client for event contracts."""

    def publish(self, payload: Mapping[str, Any]) -> ContractResult[T]:
        """Publish one event payload."""
        return self.call("publish", payload)


class ResourceClient(ServiceHandle[T]):
    """Typed client for resource contracts."""

    def read(self, payload: Mapping[str, Any]) -> ContractResult[T]:
        """Read through the provider boundary."""
        return self.call("read", payload)
