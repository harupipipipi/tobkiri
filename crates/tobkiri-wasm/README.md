# Generic component host

`PackHost` dispatches verified components through the core Gate. Its constructor
accepts immutable bindings, verified Packs, and effective limits keyed by
`(PackId, PackHash)`. Core registers the same map against the frozen revision and
sets that revision on `CallCx` before starting execution.

Component import names, typed port signatures, core function signatures, and
core resource identities are checked before linking. Actual instance creation
is deferred until first use, after the weak Session FlowRunner bridge has been
installed. `lifecycle.init` receives only canonical, hash-verified Pack config,
uses a sticky non-interactive context, and consumes a WasmEntry grant before any
component initialization executes.

Port imports use Wasmtime's dynamic `Val` API. They preserve raw finite float
bits, map only authoritative core stream/callback aliases, and gate each typed
port call. Canonical/UI float values use declared-width scientific strings.
A stream/callback receives one WasmEntry grant when its local handle is opened;
pulls/invocations retain the original cancellation/deadline and callee limits.
Its close, cancellation, or error records one final outcome. Handle aliases
retain the same source and lease, and cannot cross Profile, principal, request,
or policy epoch. Deadlines/cancellation reclaim outstanding handles.

Use `CoreStream::close().await` when a result can be returned: it waits for
guest cancellation and the final audit write, and reports cleanup failures.
Synchronous cancellation and Drop queue cleanup on the runtime captured when
the stream started, including when called from a plain thread. Drop cannot
return an audit failure. If the owning runtime has shut down, queued cleanup
and its reaper cannot run; the original deadline and durable grant expiry still
bound authority, but a final outcome row is not guaranteed in that case.

A guest's `flow.run-flow` delegates through the weak Session bridge to the same
Flow engine as native compiled Flow steps. A missing/dead bridge fails closed.
There is one live instance per Pack. A nested call to a currently locked ancestor
fails immediately with `conflict`, rather than waiting on its own lock.

Every store has fuel, aggregate memory/table caps, a bounded stack, epoch
interruption, and bounded captured stdout/stderr. The WASI subset has empty
stdin, environment and arguments, no preopens, and no socket/HTTP interfaces.
Consequently the current scratch quota is zero bytes. Host imports implement
gated HTTP with DNS/address checks, opaque secret headers, private/public/read-
only/user-global storage, audit, host I/O, clock, and random. Proc/PTY, filesystem
grants, approval attestation UI, and a Wasm mediator remain unavailable until
their trusted host contracts and backends are implemented.
Guest `host-io.request-ui-embed` is also unavailable until the Core Session
registration/notification bridge exists. Every slot name, including `startup`,
uses that same denial; the authorized grant receives a failed outcome and the
native UI host is not invoked. Core's direct trusted startup embed is separate.

## Focused verification

Build the separately locked fixture workspace before the host integration tests:

```sh
CARGO_TARGET_DIR="$PWD/../.work/cargo-target" CARGO_BUILD_JOBS=2 CARGO_INCREMENTAL=0 \
  cargo build --manifest-path crates/test-guests/Cargo.toml --target wasm32-wasip2 --locked
CARGO_TARGET_DIR="$PWD/../.work/cargo-target" CARGO_BUILD_JOBS=2 CARGO_INCREMENTAL=0 \
  cargo test -p tobkiri-wit -p tobkiri-wasm --locked
```

The Rust fixture components exercise actual typed proxy calls, finite float
boundaries, traps, fuel/memory limits, scoped streams, callback reentry, and the
FlowRunner bridge. They use a real GateEngine and SQLite authority/audit stores;
test prompts explicitly approve only fixture declarations. No native provider
fallback or external network/model is involved.
