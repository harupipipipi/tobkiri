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
gate. The first diagnostic source (`5f55ce5c`) built its component successfully,
but its Mac consumer failed Rust compilation before executing the interpreter.
The diagnostic now has a separate Cargo feature, preserving the genuine whole
source index requirement for the original bootstrap fixture, and uses the
actual owned `CoreValue` API. Thread support remains unobserved until that
consumer executes.

The next source, `f7bf36d8`, did execute both isolated interpreter scenarios in
[Mac job 121961921501](https://github.com/harupipipipi/tobkiri/actions/runs/40435029972/job/121961921501).
Each accepted only `starting`, then trapped unreachable before `local-context`,
without guest JSON or a native deadline failure. Normal preparation/total time
was 17,573,083/17,605,017 microseconds; waiter-drop preparation/total was
16,765,357/16,792,530 microseconds. Each retained 1,989,027,405 fuel with peak
payload 17,870,063 bytes. Store retirement occurred at invoke return; thread
termination, physical cleanup and waiter-drop execution were unconfirmed or
false. The parent passed because it recorded this limitation and reaped each
process. This is not thread/context support or rejection evidence.

The next diagnostic adds fixed checkpoints immediately before and after each
runtime import (`contextvars`, `struct`, `sys`, `threading`). It preserves the
original producer, runtime imports, pre-init state and limits. A missing next
checkpoint identifies only an observed boundary, not a Python exception or
ultimate cause. Preloading or replacing those imports remains deferred.
The existing fixed 1 KiB native error buffer now streams the actual error chain,
keeps a UTF-8 prefix and reports truncation. It never creates an unbounded
intermediate string or reads retired guest DATA. Its real storage is prepaid
alongside the fixed guest record within the unchanged shared payload limit.
The chain need not include frames and cannot establish a cause it does not show.

A separate minimal WAT test now attempts same-task reentry through a
sync-lift export, sync canonical lower and the same asynchronous owned host
callback pattern. It uses an actual physical funded invocation, the same Engine
settings and original limits, with no safe-API changes or unsafe Store access.
Dynamic owned calls avoid this vendor's legacy TypedFunc allocation-refusal
stub. Nested outcome and the first call's actual completion are recorded
separately; neither is assumed before execution. A warmed external process-group
watchdog bounds a pending result without treating a future drop as termination.
This test is not an independent second task, actual CPython reentry or a
substitute for the real interpreter diagnostic. The interpreter runner's
independent-entry field remains false. The `f7bf36d8` constraint failed Rust
compilation because `CoreControl` was not imported; it yielded no reentry
observation. The import is corrected and observation now requires a successful
warm compile gate. No resident implementation is enabled. Before product adoption,
a concrete execution base must prove required same-state concurrent progress,
original context/thread/wait semantics, individual operation termination without
corrupting other operations, and continuously funded retained memory.
