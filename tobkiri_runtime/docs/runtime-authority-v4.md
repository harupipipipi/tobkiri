# Runtime authority v4 integration

This module is the canonical runtime implementation of the authority and effect
lifecycle defined by ADR-014 and the SecurityEpoch/audit portions of ADR-015. It is
implemented under `core_runtime.authority.v4` and remains unreachable from legacy
`AuthorityService`, `CapabilityExecutor`, `FunctionRegistry`, and HTTP dispatch.
The canonical `tobkiri_host.authority_v4.AuthorityV4Adapter` is the only Pack v4
bridge. It must be explicitly constructed by a Host-owned composition root; no
legacy or incomplete backend is enabled by importing it.

## Security boundary

The implementation keeps these records distinct:

- `FunctionPrincipal` is an exact parent artifact, Function implementation,
  Contract revision, Function ID, and Operation ID.
- `ExecutionDomain` is a Host-assigned process/component boundary with an
  authenticated channel, boot epoch, ResourceHandle namespace, fencing token, and
  SecurityEpoch.
- `ApprovalRecord` is immutable decision provenance and is never evaluated as
  runtime authority.
- `HostExtensionTrustRecord` permits one exact Host Extension artifact to register
  only its explicitly listed Provider Function principals; it is not Pack-wide
  authority.
- `ProviderAuthorityRecord` is the exact target Provider ceiling.
- `GrantRecord` is exact immediate-caller use authority for one Profile activation.
- `InvocationLease` is short-lived, MAC-authenticated, request-bound, single-use,
  and never exposes the Grant to the Provider.

Scopes use explicit capability and semantics digests, finite canonical dimensions,
and quota upper bounds. An omitted dimension is unbounded, like an explicit `*`, and
an omitted quota has no upper bound. Therefore omission or wildcard in a request is
never a subset of a corresponding bounded ceiling; finite restrictions are subsets
of omitted/unbounded ceilings. Intersection retains the union of restricted keys and
applies set intersection or the smallest quota, so an omitted field cannot erase a
bound from another ceiling. Missing or mismatched semantics deny. Opaque semantics
are limited to exact-request one-shot Grants. Final authorization requires the
concrete request to be within the caller Effect, immutable runtime safety,
Profile/admin, caller Grant, and Provider ceilings.

Authority records and audit payloads are Fernet-encrypted in a SQLite database. WAL
with `synchronous=FULL` and `BEGIN IMMEDIATE` transactions make Grant-use reservation,
authoritative audit reservation, and Lease issuance atomic. Audit failure rolls the
whole transaction back. The audit journal has a monotonic sequence and previous-event
digest chain. A crash after dispatch is recovered as `ambiguous`, never automatically
retried.

Schema migration is fail closed. Before upgrading the historical v1 lease table to
schema v2, startup verifies the exact known table/column topology and the complete
encrypted audit hash chain. Unknown versions, partial schemas, missing tables, and
corrupt audit rows are rejected without normalization. The v1-to-v2 change is one
transaction: unused legacy leases become `revoked`, dispatched legacy effects become
`ambiguous`, Grant-use counters are reconciled, audit events are appended, and only
then is the schema version advanced. Audit failure rolls back the entire migration.

## `tobkiri_host` call surface

The Host package imports only from:

```python
from core_runtime.authority.v4 import (
    AuthorityKernel,
    AuthorityKernelProtocol,
    AuthorityStore,
)
```

It constructs `AuthorityStore` in the Host-owned security-state directory and
constructs `AuthorityKernel` with an `AuthorityBindingResolver`. The resolver must
read the already-captured `ResolvedPlan` and `ActivationRecord`; Pack payload fields
must never implement this callback.

The integration sequence is:

1. `register_execution_domain(...)` after verified Host spawn and authenticated
   channel establishment. Registration durably moves `starting -> active` before the
   session becomes usable.
2. `commit_approval_bundle(...)` for an atomic HostExtensionTrust, Approval,
   ProviderAuthority, and Grant ceremony, or `commit_policy_ephemeral_grant(...)`
   for a restrict-only low-risk decision. Non-expanding updates mint new exact
   records and use `commit_successor_authority(...)`; old artifact records are never
   matched directly.
3. `authorize(context, request_scope)` after static admission, runtime evidence, and
   exact target selection. The caller comes from the registered session, never from
   `InvocationContext` payload identity.
4. Pass only `AuthorizationResult.lease_token` and opaque resource namespace to the
   selected Provider.
5. `dispatch(...)` at the final Broker effect boundary. This atomically consumes the
   Lease and rechecks epoch, revocation, request, domain, boot epoch, and target
   principal.
6. `finish(..., state=committed|failed|ambiguous, ...)` after the effect outcome is
   known. Call `recover()` during Host recovery to mark interrupted dispatched
   effects ambiguous.
7. `revoke(...)` and `advance_security_epoch(...)` for emergency fencing. The Host
   must supply a `terminate_domain` callback that kills dedicated entitlement
   processes and other affected domains after durable fencing.

The Host `RequestContext` passed to this bridge includes the authenticated caller
session/domain/boot epoch, exact target domain/boot epoch, ProfileAuthoritySnapshot
digest, and activation fencing token. Opaque principal references resolve through a
Host-owned captured-plan resolver and must equal the resolved exact principal digest.
The adapter rejects aliases and never reads identity from the invocation payload.

The adapter also implements `AuditPort`. Authorization already creates the audit
reservation atomically with Grant-use reservation and Lease issuance; `reserve_effect`
therefore returns that canonical reservation, boundary recheck performs canonical
`dispatch`, and audit completion performs canonical `finish`. `recover()` marks any
crash-surviving dispatched effect ambiguous.

The Provider receives no `ApprovalRecord`, `ProviderAuthorityRecord`, or
`GrantRecord`. `os_entitlement` Providers require a single-principal dedicated
process. Multi-principal native co-location is rejected unless the domain has an
explicit complete `AuthorityEquivalence` record and mutual principal approval.

## Defaults Pack catalog read authority

The control panel's Pack list is one selected v4 contribution, not an ambient
Host API. The locked Defaults Profile contains exactly one edge from the
`shell.tauri.pack-control` caller Function to the
`tobkiri.host.pack-control.v4/catalog.read` Provider Function. Explicit Defaults
activation confirmation commits only that read scope, bound to the exact Profile,
activation, ResolvedPlan digest, ProfileAuthoritySnapshot digest, SecurityEpoch,
caller and Provider principals, and dedicated Provider domain. Every request still
receives a single-use InvocationLease and authoritative audit lifecycle.

The Provider accepts an empty object and returns the canonical catalog projection.
It has no network, process, secret, workspace-write, installation, approval,
enablement, disablement, revocation, or restart operation. Those mutation
ceremonies are separate operations and are absent from this Defaults authority;
an authenticated panel session alone cannot invoke them. Missing confirmation,
stale activation state, a changed epoch or principal/domain binding, revocation,
or lease replay fails closed through the normal Broker and Authority Kernel path.

## Deliberate integration limits

- This module does not define serialized Protocol JSON Schema; that remains owned by
  the protocol/schema implementation.
- It does not look up Providers from live registries. The Host resolver supplies an
  exact captured binding.
- It does not install itself into legacy dispatch or accept legacy hierarchical
  principal/global Grant fallback.
- ResourceHandle and credential material remain opaque integration inputs; this
  authority kernel binds their namespace but does not expose raw paths or secrets.
- Production backends remain unreachable until their independent backend status has
  every required gate and explicitly leaves conformance-only mode.
