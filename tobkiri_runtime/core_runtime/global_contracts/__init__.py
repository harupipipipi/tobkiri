"""Vocabulary-neutral typed global contract foundation."""

from .canonical import canonical_json, content_identity
from .clients import ActionClient, EventClient, ResourceClient, ServiceHandle
from .models import (
    Cardinality,
    ContractDescriptor,
    ContractRequirement,
    ContractResult,
    ContractStatus,
    FailureSemantics,
    LifecycleMetadata,
    ProviderDescriptor,
    SecurityClassification,
)

__all__ = [
    "Cardinality",
    "ActionClient",
    "ContractDescriptor",
    "ContractRequirement",
    "ContractResult",
    "ContractStatus",
    "FailureSemantics",
    "EventClient",
    "LifecycleMetadata",
    "ProviderDescriptor",
    "SecurityClassification",
    "ResourceClient",
    "ServiceHandle",
    "canonical_json",
    "content_identity",
]
