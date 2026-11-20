# Owned runtime checkpoint

This is a source checkpoint, not a whole-memory or SQL runtime acceptance receipt.
The prior 33 WIT/Wasm tests and M2 model receipts describe the earlier source
snapshot. The active Wasmtime fork, opaque Flow APIs and new source below have
not been built together under the current build hold.
The parent's later pure foundations checkpoint passed the nine capacity-ledger
tests, including preparation retry and physical release ordering. That receipt
does not cover the subsequent Wasm frame/bootstrap/provider-entry source.

## Implemented source boundaries

* Stable physical ledger keys are Profile+Pack or Profile+established User.
  Hash, revision, epoch, store and request are provenance, not new funds.
* `CapacityBinding` is real preparation occupancy. Its final drop releases the
  broker proof. A failed reservation leaves the same guard usable for retry.
* A physical reservation uses Canon `LeasedShared<T,R>`. Payload and shared
  block deallocation precede release-plan accounting. Admission/release share
  the owner mutex, and remaining record/root-proof retirement completes before
  another admission can observe the refund. Clones share one physical charge.
* Each reservation conservatively prepays 4096 bytes for its exact shared block,
  bounded ID copies and record/node slack. The layout is checked before creation.
* The allocation adapter prepays 32 KiB of adapter/context/control metadata.
  Fixed private custody slots keep its final strong Arc and an external physical
  reservation. Only actual final reference/control destruction releases that
  charge; elapsed time is never proof of cleanup. A trusted accidental Weak
  keeps the slot quarantined. Factory/reaper use the same slot mutex.
* Frame-backed physical reserve/growth uses Gate's fixed synchronous admission
  closure under the same Core node-state lock as retirement. A limit snapshot
  cannot admit an allocation. The reported retained proof byte count is only
  inventory; actual Core factory/control prepayment must precede its allocation.
* CoreValue/Val conversions retain owned graphs and budgeted component payloads.
  Scalars use bounded stack formatting; bytes use prepaid CoreBuffers; typed
  record/flag searches precharge work before lookup. Unchanged raw scalar
  projection preserves float bits; finite-only canonical textual UI mapping
  remains separate.
* Native Input shell funding resolves the verified source Pack/hash and exact
  view, operation and provided endpoint. Gate rechecks this exact source binding
  and current approval before every allocation. Funding does not change the
  Input principal or mint an entry/port grant.
  The source-to-endpoint binding comes only from the explicit frozen Profile
  input record; an opaque source ID is never parsed or compared as a Pack ID.
* Handle cleanup and terminal delivery are separate receipts. Cleanup is attempted
  once; failed delivery retries its immutable proposal. Due consumed permits use
  Gate deadline settlement and the durable terminal winner. Callback/retired
  generation cleanup that is not proved remains closed and quarantined.

## Coverage still unavailable

The following are not accepted as aggregate-memory proof and must not be enabled
through a fallback: initial ledger/owner/preparation map and Arc registry custody;
active producer-frame allocation authority and its borrowed bootstrap/control
custody; runtime queues/tasks/host closures;
JIT/module compilation; instance linear/table/stack aggregate reservations and
physical retirement; every native WASI/resource-table/destructor path; resource
identity across exact Core versions; reqwest/network/storage/audit intermediate
buffers; and the complete owned facade/common admitted-dispatch integration.

The source still contains earlier CanonValue/String/Vec runtime paths pending
migration. The fork denies unsupported static/custom component allocations and
ordinary dynamic async registration; those denials cannot be replaced with the
old registration APIs or a native fallback to obtain a passing runtime receipt.

The earlier capacity checkpoint (`ec8a0688f4`) recorded rejection before
Engine/JIT. The current [PackHost constructor](src/host.rs) prepares a bounded,
verified catalog as native Core control work. It checks Pack, view, binding,
WIT, and artifact bounds, creates the Engine under the control context, and
compiles verified components with effective limits for their exact Pack
revisions. A missing revision limit causes an audited denial before compilation.
Guest entry separately requires Gate authorization and the applicable Core
entry, storage, and ABI allocation paths. Missing prerequisites or unsupported
operations fail at entry. The catalog bounds and successful compilation leave
aggregate memory coverage open, including JIT memory and guest instance costs.

An opaque payload alone does not account for async_trait boxes, task queues,
resource table control storage or retained physical I/O.

The Core-private execution factory will issue reached-frame/node/reason evidence
for allocation. Registered owners and reusable witnesses alone are insufficient.
Producing-owner approval funds that owner; unrelated effective-principal memory
caps are not automatically substituted. Explicit transitive constraints apply
separately. Off-owner frame budget lookup remains unavailable until this sealed
boundary exists.

The [Wasm provider dispatch](src/dispatch.rs) requires the Core provider-entry
bridge. It validates the request before claiming the original port admission,
then uses the same dispatch for entry. Aggregate physical coverage for the
factory, owned futures, and terminal receipts still requires evidence across
the complete entry path.

## SQL producer boundary

`tools/wasm-sqlite` pins public-domain SQLite 3.53.2, SDK34, numeric ABI, flags,
source and producer identity. The refreshed private 1,073,130-byte module has SHA256
`d52e4d9ec0ee1d6bee0cb26918d907d96722442950787a5c45656d674e3ccc02`.
This proves only production of the pinned internal module. It does not prove
exclusive DB/journal/WAL recovery, fuel/cancellation/conversion, statement
classification barriers, transaction cleanup or host marshal/VFS capacities.
No SQL backend acceptance or whole-runtime claim follows from this artifact.

With the opt-in `private-sqlite-experiment` feature, the
[build script](build.rs) requires `TOBKIRI_SQL_PRODUCER_DIR`, verifies the
locked identity and digests, and fails for missing or mismatched inputs.
Builds with the feature disabled skip producer validation. The experiment uses
only the verified producer output.
