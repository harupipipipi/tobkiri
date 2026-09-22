"""Public, narrow ADR-014/015 authority surface for ``tobkiri_host``.

This module is intentionally not imported by the legacy AuthorityService or any
legacy dispatch path.  A Host integration must explicitly construct an
``AuthorityStore`` and ``AuthorityKernel`` and provide a trusted
``AuthorityBindingResolver`` backed by the captured ResolvedPlan/Activation.
"""

from .v4_kernel import (
    AuthorityBinding,
    AuthorityBindingResolver,
    AuthorityKernel,
    AuthorityKernelProtocol,
    AuthorizationResult,
    mint_successor_grant,
    mint_successor_provider_authority,
)
from .v4_models import (
    ApprovalRecord,
    AuthorityDenied,
    AuthorityEquivalence,
    AuthorityMode,
    AuthorityScope,
    AuthorityValidationError,
    DomainBoundary,
    DomainState,
    ExecutionDomain,
    FunctionPrincipal,
    GrantLifetime,
    GrantRecord,
    HostExtensionTrustRecord,
    InvocationContext,
    InvocationLease,
    LeaseState,
    ProviderAuthorityRecord,
    SecurityEpoch,
    SuccessorEvidence,
    UpdateTrustPolicy,
    authority_digest,
    intersect_scopes,
)
from .v4_store import AuditUnavailable, AuthorityStore, AuthorityStoreError


__all__ = [
    "ApprovalRecord",
    "AuditUnavailable",
    "AuthorityBinding",
    "AuthorityBindingResolver",
    "AuthorityDenied",
    "AuthorityEquivalence",
    "AuthorityKernel",
    "AuthorityKernelProtocol",
    "AuthorityMode",
    "AuthorityScope",
    "AuthorityStore",
    "AuthorityStoreError",
    "AuthorityValidationError",
    "AuthorizationResult",
    "DomainBoundary",
    "DomainState",
    "ExecutionDomain",
    "FunctionPrincipal",
    "GrantLifetime",
    "GrantRecord",
    "HostExtensionTrustRecord",
    "InvocationContext",
    "InvocationLease",
    "LeaseState",
    "ProviderAuthorityRecord",
    "SecurityEpoch",
    "SuccessorEvidence",
    "UpdateTrustPolicy",
    "authority_digest",
    "intersect_scopes",
    "mint_successor_grant",
    "mint_successor_provider_authority",
]
