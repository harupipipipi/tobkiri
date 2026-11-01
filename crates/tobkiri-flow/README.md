# Tobkiri Flow

The single interpreter executes the compiled Profile snapshot supplied by
`tobkiri-profile`. It performs no provider discovery and owns no filesystem,
network, process, SQL or wasm effect implementation.

Every call, overlay call, `next` dispatch and stream emission uses `PortInvoker`.
Production invokers resolve the frozen binding, authorize through the core gate,
audit and execute. The gate is not an editable Flow step.

The engine supports calls, subflows addressed by `implements`, branches, bounded
parallel execution and bounded loops. Parallel results retain input order. Each
run retains its original cancellation token, deadline, request ID and plan
digest. Each loop can narrow its Profile limits. Cost units count dispatched
port operations and emitted stream values; they are execution units, not money.

Streams are pull-based. Each value is emitted and optionally collected before
the next pull. The run's output byte ceiling bounds retained stream data. Dropped
streams cancel their source. Result-bearing consumers await `close`, which
surfaces grant cleanup and audit failures. The default source close is for pure
sources; effect-owning implementations must override it. Void Drop cannot return
an error and requires the owning runtime to remain alive for asynchronous cleanup.
Provider failures and panics become step errors;
`on_error` selects failure or continuation. Cancellation is never continued.

Overlays are already ordered and authorized by the Profile compiler. The
interpreter enforces `next` at most once. Explicit `return` replaces the result;
fall-through after `next` preserves its result; a body without either deletes
the step. Compile-time checks decide whether a deleted output may be omitted.

`RunJournal` persists every transition before it is delivered to a run's event
receiver. The Core audit adapter stores value digests rather than guest values
or secret plaintext.

Run the focused acceptance tests with `cargo test --locked -p tobkiri-flow`.
