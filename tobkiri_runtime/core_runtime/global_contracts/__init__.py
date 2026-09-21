"""Vocabulary-neutral typed global contract foundation."""

from .canonical import canonical_json, content_identity
from .clients import ActionClient, EventClient, ResourceClient, ServiceHandle
from .legacy_projection import LegacyProjectionRule, LegacyRegistryProjection
from .manifest import ManifestDiagnostic, load_manifest
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
from .semver import is_compatible, parse_version, validate_version_range

__all__ = [
    "ActionClient",
    "Cardinality",
    "ContractDescriptor",
    "ContractRegistry",
    "ContractRequirement",
    "ContractResult",
    "ContractStatus",
    "EventClient",
    "FailureSemantics",
    "LegacyProjectionRule",
    "LegacyRegistryProjection",
    "LifecycleMetadata",
    "ManifestDiagnostic",
    "ProviderDescriptor",
    "ResourceClient",
    "SecurityClassification",
    "ServiceHandle",
    "canonical_json",
    "content_identity",
    "is_compatible",
    "load_manifest",
    "parse_version",
    "validate_version_range",
]
