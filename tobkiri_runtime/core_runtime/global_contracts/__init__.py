"""Vocabulary-neutral typed global contract foundation."""

from .canonical import canonical_json, content_identity
from .manifest import ManifestDiagnostic, load_manifest
from .clients import ActionClient, EventClient, ResourceClient, ServiceHandle
from .legacy_projection import LegacyProjectionRule, LegacyRegistryProjection
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
from .registry import ContractRegistry

__all__ = [
    "Cardinality",
    "ActionClient",
    "ContractDescriptor",
    "ContractRegistry",
    "ContractRequirement",
    "ContractResult",
    "ContractStatus",
    "FailureSemantics",
    "EventClient",
    "LegacyProjectionRule",
    "LegacyRegistryProjection",
    "LifecycleMetadata",
    "ManifestDiagnostic",
    "ProviderDescriptor",
    "SecurityClassification",
    "ResourceClient",
    "ServiceHandle",
    "canonical_json",
    "content_identity",
    "load_manifest",
]

