"""Typed consumer clients over opaque provider handles."""

from __future__ import annotations

from dataclasses import dataclass
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
    _registry: ContractRegistry

    def call(self, operation: str, payload: dict[str, Any]) -> ContractResult[T]:
        """Invoke a provider operation through the registry boundary."""
        return self._registry.invoke(self.provider_instance_id, operation, payload)


class ActionClient(ServiceHandle[T]):
    """Typed client for action contracts."""


class EventClient(ServiceHandle[T]):
    """Typed client for event contracts."""

    def publish(self, payload: dict[str, Any]) -> ContractResult[T]:
        """Publish one event payload."""
        return self.call("publish", payload)


class ResourceClient(ServiceHandle[T]):
    """Typed client for resource contracts."""

    def read(self, payload: dict[str, Any]) -> ContractResult[T]:
        """Read through the provider boundary."""
        return self.call("read", payload)

