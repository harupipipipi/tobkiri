# Workflow v4 provider integration

`core_runtime.workflow_v4` is the Phase 13 backend boundary. It does not
register legacy Functions, restore an Interface Registry, expose Pack-specific
HTTP routes, or call `flow_scheduler.py`. The Host composes it after Profile
resolution and exposes its finite Function principal through the normal v4
broker.

## Canonical identity

- Pack: `tobkiri_workflow_pack`
- Contract: `tobkiri.workflow.v4`
- Function principal: `tobkiri.workflow.provider`
- Executable: `ecosystem/tobkiri_workflow_pack/runtime/provider.py`
- Local state: a Host-selected SQLite path plus its mode-0600 seal key

The generated `contracts.v4.json`, `executables.v4.json`, `pack.v4.json`, and
`artifact-index.v4.json` are produced by
`ecosystem/tobkiri_workflow_pack/generate_v4.py` through the repository's
official Pack and executable renderers. The same generator registers the Pack
in `schemas/pack_v4_catalog.v1.json` and `schemas/manifest_authority.v1.json`.
Generation is deterministic and validates every result against the repository
v4 schemas. `backend-integrity.v4.json` pins all imported dedicated backend
modules into the Pack artifact set; the executable verifies it before import.

## Host provider API

The integration hook constructs `WorkflowStoreV4` and `WorkflowEngineV4`, then
registers `WorkflowProviderV4.invoke(operation_id, payload)` as the sole
implementation of the resolved `tobkiri.workflow.provider` Function principal.
It must inject four captured providers:

1. `ContractCatalogProvider.snapshot()` returns the active catalog digest,
   SecurityEpoch, ActivationRecord identity, and exact bindings containing
   Contract ID, revision digest, Operation ID, Function principal, Provider ID,
   input schema digest, and effect ceiling.
2. `AuthorityProvider.reserve/inspect/commit/finish/revoke` maps to the Host
   Authority v4 lifecycle. `commit` must return an ephemeral one-dispatch token;
   the workflow store never persists it.
3. `ContractInvocationProvider.invoke/cancel` sends an exact Contract Request
   through the Host broker. This is not a direct Provider callback.
4. `InputValidator.validate` resolves catalog-owned schema digests locally. It
   must not fetch schemas from the network.

The provider Contract operation list is exported as `WORKFLOW_OPERATIONS` from
`core_runtime.workflow_v4.provider`. Consumers must resolve these operations
from Pack v4 artifacts rather than construct route names.

`frontend_contract_map.v4.json` is the generated Shell integration input. It
contains an exact target for Definition CRUD/archive/publish/validate/compile,
the active operation palette, Run create/read/advance/pause/resume/cancel, and
Step execute/retry/resume/reconciliation. A Shell/Profile generator may compose
these targets only after this optional Pack has passed install, approval,
enablement, and the Profile change ceremony. The Workflow Pack does not mutate
the defaults Profile, lock, plan, or the active Launcher map directly.

## Authority and recovery invariants

Each attempt reserves authority bound to workflow ID, published revision
digest, run, step, attempt number, exact request/effect digests, call chain,
idempotency key, Function principal, ActivationRecord, and SecurityEpoch.
Approval continuation re-inspects that reservation. Commit occurs immediately
before broker dispatch and is rejected if the reservation, request digest, or
epoch changed.

Checkpoints contain request/effect digests, call chain, idempotency key, opaque
reservation ID, and SecurityEpoch. Invocation leases, dispatch tokens,
credentials, and unbounded Host handles are rejected. A crash with an attempt
in `dispatching` or `running` becomes `ambiguous_effect` and moves its Run to
`needs_reconciliation`; it is never retried automatically.

`run.advance` executes dependency-ready steps with the published bounded
concurrency setting. A Scheduler Pack should call `run.create` with an
occurrence ID after the Host wake kernel admits delivery. The store claims
`occurrence ID + workflow revision digest` atomically, preventing duplicate
starts without importing Scheduler domain logic into the Runtime TCB.
