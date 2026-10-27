# Native execution-base capability investigation

The actual whole component now initializes and executes its original Profile-id
and both root getters under the existing limits. The
[component boundary](native-harness-path-bridge.md) records the exact source,
artifact and execution evidence. This does not establish the remaining original
operations or the full DMG/Setup/approval/settings/conversation flow.

The original callees retain module/class/exception identity, issued origins,
ContextVar/thread custody, ActivationStore retry state and shared flight/waiter
synchronization. A resident wrapper, serialized calls, fresh Stores or destroying
the entire Store on cancellation must not be treated as equivalent without proof.
The current generated exports are synchronous; the compiled Wasmtime feature
evidence and its exclusive invocation API do not establish independent calls
into the same state. Dropping a waiter also does not prove task cancellation.

The separate `harness-thread-capabilities` component investigates actual CPython
SDK behavior needed to choose an execution base. It uses the same authenticated
producer and stub-WASI recipe, exact pinned source/WIT, the common cold
preparation path and original 30 seconds / 64 MiB / 2 billion fuel / 4 MiB
envelope / 32 MiB source bounds. It never creates an application Profile, grants
native effects or changes the product default. Its small compile cost cannot
be extrapolated to the whole component.

The diagnostic calls real stdlib `Thread`, `Event`, ContextVar set/reset and
cross-context token operations during actual guest execution, then observes
cooperative stop and join. Neither headers nor thread IDs alone prove a real
worker. Failure stage, actual available exception data, native trap/deadline,
Store retirement and thread termination remain separate observations. A runtime
trap must not be converted into an invented Python exception. Start refusal,
confirmed Event/join, possible unconfirmed worker and unclassified runtime or
process failure are distinct outcomes.

Normal and waiter-drop scenarios each run in a separate process and fixture.
The latter retains the original invocation future while polling and dropping
only its borrowing waiter; it then releases the same native checkpoint gate so
the real operation can attempt cooperative stop and join. It tests neither
dropping the invocation nor individual Wasmtime task cancellation. Unsupported
thread creation may leave that waiter path unexercised; the result says so.

The kernel's existing error-retirement policy stays intact. An unconfirmed
worker or outer failure permits only minimal host recording and process exit,
with no further Wasm call or reuse. Guest DATA returned from an already-retired
Store is not used as state-safety evidence. Known output is fixed in separately
funded host storage before explicit retirement. Additional caller error paths
retain the program without an ordinary destructor until process termination.
The parent reaps its actual child before fixture cleanup and has a separate
40-second isolation watchdog. This never renews the operation's 30 seconds;
forced process termination is not a successful join, individual cancellation or
product recovery proof. Isolation does not prove that retiring an interpreter
with unconfirmed threads is safe.

The workflow and receipts are explicitly diagnostic-only. A passing diagnostic
job may record an unsupported capability; it does not waive the final migration
gate. The second-sync-export constraint observation remains unimplemented and
is explicitly false in the current runner, even though `ping` is available for
that later test. No resident implementation is enabled. Before product adoption,
a concrete execution base must prove required same-state concurrent progress,
original context/thread/wait semantics, individual operation termination without
corrupting other operations, and continuously funded retained memory.
