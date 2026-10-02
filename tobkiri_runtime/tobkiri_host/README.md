# Tobkiri v4 host execution core

This package is the isolated Python reference implementation of the canonical Pack
v4 execution path. It does not extend or fall back to `FunctionRegistry`,
`InterfaceRegistry`, host Python loading, Pack-specific HTTP routes, or defaultspack.

Its owned responsibilities are:

- exact `PackArtifact` and Function subartifact inventories;
- resolved-plan-only Contract Operation routing;
- pure, bounded, capability-free structural Adapter planning;
- opaque, descriptor-backed `ResourceHandle` binding and revalidation;
- backend selection with all production safety gates disabled by default;
- lazy, principal-separated single-flight materialization;
- static admission, fair bounded queues, and resource reservations;
- canonical Request execution, deadline fencing, `ambiguous_effect`, and explicit
  reconciliation;
- the minimal Trigger/Wake Kernel; and
- orthogonal Base Pack and `app.shell.v1` resolution.

## Security-core integration boundary

`tobkiri_host` does not define Grant, principal, InvocationLease, or ExecutionDomain
semantics. Those canonical types and their persistence belong to
`core_runtime.authority`. This package transports only bounded opaque references.

An adapter from `core_runtime.authority` must implement `AuthorityPort` exactly:

- `check_static_path(query)`
- `authorize_and_issue_lease(query)`
- `recheck_effect_boundary(context, target, lease)`
- `fence_request(request_id)`
- `issue_trigger_lease(registration_id, occurrence_id, target, security_epoch)`

The production-shaped implementation is `AuthorityV4Adapter`. Construct it with an
`AuthorityKernel` and a Host-owned `PrincipalReferenceResolver` backed only by the
captured ResolvedPlan/Activation. The `RequestContext` must carry exact authenticated
caller session/domain/boot and target domain/boot bindings, the
ProfileAuthoritySnapshot digest, and the activation fencing token. Principal aliases
are rejected.

The authoritative audit implementation must implement `AuditPort` exactly:

- `reserve_effect(context, binding, request_digest)`
- `mark_dispatched(reservation)`
- `commit_effect(reservation, outcome_digest)`
- `fail_effect(reservation, stable_code, ambiguous)`

All exceptions from these ports fail closed. A backend is production-selectable only
when `production_enabled` is true, `conformance_only` is false, and every gate in
`REQUIRED_PRODUCTION_GATES` is present. There is no weaker backend fallback.

`AuthorityV4Adapter` implements both ports against one v4 authority-store lifecycle;
it does not create a secondary best-effort audit log. Call `recover()` at Host startup
before accepting requests. Trigger support additionally requires a Host-owned durable
`TriggerAuthorityResolver`; without it trigger lease issuance is denied.
