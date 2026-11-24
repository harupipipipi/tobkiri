> Resource policy corrections (2026-10-15): [resource-boundaries.md](resource-boundaries.md) governs native Core/Wasmtime control and compile/load accounting. Ordinary bounded Wasmtime constructors and native SQLite are permitted; universal native allocation interception and SQLite-in-Wasm are optional. Guest-sized data keeps the original owner/lifetime rules.

# Issue 1520 Rust implementation contract

[Issue 1520](https://github.com/harupipipipi/tobkiri/issues/1520) is the normative
requirement. This document records the final design decisions returned by Devin
CLI `claude-opus-5-5-medium` in design-only `-p` sessions on 2026-10-15. Later
decisions below supersede the preliminary design. The user subsequently replaced
the design adviser with Astra, initially at max and then at high effort, for
question answers only. New consultations use high;
[astra-answers.md](astra-answers.md) supplies the later authority and recovery
decisions. This is an implementation contract, not a migration-completion claim.

## Runtime and ownership

The Rust host workspace contains canon, WIT, manifest, Profile, Flow, gate, store,
wasm, core, CLI and e2e crates. `tobkiri-core` is the facade shared by hosts. Flow
depends on generic types and gate abstractions, and cannot import Wasmtime,
SQLite, an HTTP client or an effect implementation. One Flow engine executes
input, subflows and guest `flow.run-flow` calls. The host supplies trusted prompts,
display, terminal I/O, directory selection and isolated Pack UI embedding.

All domain shapes, prompts and procedures belong to Packs. No mechanism or host
source may name a Pack or a Pack-owned shape. New unsigned development Packs use
`local.*`; publisher names require a verified signing-key binding. A name with a
different key is a conflict. Pack verification is independent of any bundle.

The core WIT namespace is `tobkiri:core`, with exactly these 17 interfaces:

`types`, `network`, `fs`, `proc`, `pty`, `clock`, `secrets`, `random`, `storage`,
`gate-mediator`, `approvals`, `flow`, `lifecycle`, `stream-source`, `callback-sink`,
`audit`, `host-io`.

`pack-minimal` imports types, Flow, clock, random and audit, and exports lifecycle.
Other generic imports are selected by declared permissions. Actual component
imports and signatures must match declared, verified WIT; ambient sockets,
HTTP, environment variables and directory preopens are unavailable. Guest
components live in independently locked WASI workspaces.

## Bytes, files and shapes

New documents use StrictV1 canonical JSON: duplicate keys, floats, unsafe integer
numbers, invalid Unicode, depth above 64 and inputs above 4 MiB are rejected.
Legacy canonical and signing recipes remain separate byte-compatibility domains.
Frozen Python outputs are the oracle; changing expected bytes to match Rust is
forbidden. Each persisted legacy recipe needs its own coverage before deletion.

WIT f32/f64 values cross JSON as finite scientific strings produced by Rust's
`format!("{:e}", value)` at the declared width. Negative zero stays `"-0e0"`.
Decode, reformat and compare exactly; numeric JSON, noncanonical alternatives,
NaN and infinity are rejected. Raw outbound provider body bytes can contain the
external provider's normal JSON number representation.

Shape names are `publisher.pack.seg(.seg)*`, with kebab-case path segments, at
most eight path segments and 128 characters total. The first two segments are
the owner Pack ID. WIT interfaces join the path segments with `-`; colliding
names are rejected locally. The dotted name is authoritative, and reverse
mapping uses the shape registry. Each shape has an independent versioned WIT
document. Only `tobkiri:core` is reserved.

WIT is the type authority. Minor changes must be additive and projection-safe;
major changes require an adapter Pack. Required connections, one-slot ambiguity,
duplicate IDs, incompatible versions and unordered overlays fail compilation
without changing the Profile head.

All component, WIT, config, seed, UI and provenance bytes are manifest hash-bound.
`runtime.config = {"path":"config.json", "shape":<optional shape>}` supplies
immutable StrictV1 object config, at most 256 KiB, to `lifecycle.init`. With no
config, init receives `{}`. There is no host, CLI or Profile config override map.
Mutable settings use typed Pack ports and private stores. Init is idempotent and
non-interactive. Seeds are installed according to the approved tier/update rules;
published Profiles do not contain private data or approvals.

## Gate, context and guest execution

Effective authority is the intersection of manifest declaration, Profile ceiling,
per-Pack limits and Profile/hash approval. Grants are opaque, noncloneable,
single-use and bound to principal, request, effect digest and persistent security
epoch. SQLite CAS consumption and durable epoch checks apply across processes.
Payload `approved`, Pack identity and UI identity never mint authority.

Resolve original authority sources independently in the frozen exact-view
registry. Bindings create topology, never rights. Missing explicit limits and
absent/empty allowed ports deny. Optional User policy and its document/revision/
epoch approvals use their own domain. Verified literal compiled calls use a
Core-private, one-activation witness rather than a guest-visible origin flag;
dynamic guest calls still require consumes. Final dispatch shares one grant and
charges the inherited Flow scopes once. The detailed follow-up rules are in
[astra-answers.md](astra-answers.md).

`CallCx` inherits the compiled-revision digest, cancellation, deadline and a
sticky non-interactive flag. Children increment depth, bounded at 32, and carry
the executing `(PackId, PackHash)` ancestry. A nested call into an ancestor's
locked instance uses a fresh available instance or fails immediately with
conflict; it cannot wait for the ancestor lock.

Gate `WasmEntry` covers init, stream registration, callback registration and
mediator review. Before instantiation, the exact Pack/hash must be registered in
the frozen revision, approved in the Profile and present in the effective
resource map. Failure is audited and instantiates nothing. Init cannot prompt.
Every guest entry observes the callee's fuel/memory limits and the lesser of the
caller deadline and callee wall limit.

Streams use scoped pull handles. Registration consumes one grant; pulls check a
live handle and inherit bounds without generating one audit row per item. Close,
cancel or error records one terminal outcome. Handles cannot cross Profile,
principal, request or epoch. Cancellation has a bounded grace period.

`FlowRunner` lives in the pure Flow crate. Session implements it using one engine
and one frozen revision. PackHost receives a `Weak<dyn FlowRunner>` once; a dead
reference returns unavailable. Both engine subflows and guest run-flow use that
bridge and the `FlowRun` gate request. `implements` must resolve uniquely in the
context's revision, never the current mutable Profile head.

Native process and PTY operations remain unavailable until the already-approved
typed request fields and core-built, redacted trusted summary exist. Native Pack
execution and cross-Profile storage require explicit dangerous permission and a
special trusted warning. Returning unavailable is not fulfillment of those
requirements.

## Storage, UI and publication

Profile-local SQLite stores have private, public and read-only tiers. Public
writing is approved once per writer Pack and store pair. Orphan data remains
visible; transfer needs matching data shape and user approval. Files and SQLite
journals use directory-FD capability resolution rather than pathname checks.
Secrets are encrypted with Profile-bound AAD and cannot be exported by Packs.
Audit is append-only, hash-chained, bounded and free of plaintext secret values.

Untrusted SQL requires the private bounded Wasm backend and capability VFS;
native arbitrary SQL fails unavailable before preparation. Core 1.1 adds lossless
typed results and transactions without altering Core 1.0 or adding an interface.
Guest, SQL, ABI, host/VFS and retained output allocations share stable owner
funds and allocation-owned leases. A pinned Wasmtime source patch supplies the
supported preallocation hooks. A physical-root registry and proven OS ownership
lock prevent new Core/process instances from creating extra funds. These are
contracts to prove, not claims that an artifact or call-wide guard proves the
whole memory bound.

Terminal grant decisions and immutable audit events commit atomically in the
approval database's bounded outbox. The separate audit sink deduplicates event
identity and rejects conflicting bytes; delivery acknowledgement is retryable.
Startup recovery and a runtime deadline supervisor use the persisted original
deadline. Explicit close reports durable-write, cleanup and delivery errors.
Drop does not certify audit delivery or physical resource release.

Published Profiles include pinned Packs, ceilings, overlays and startup settings;
they exclude every store tier, approvals, secrets and global user storage.
Import verifies and admits each Pack again and obtains fresh approvals.

The approved Launcher design embeds Pack UI from a custom scheme into a sandbox
with scripts allowed and same-origin unavailable. The host frontend holds the
MessageChannel and IPC permission; Pack frames have no host IPC. The core mints
the UI session, restricts calls to `ui.ports_used` and gates every port call.
Launcher replacement and browser isolation verification are still pending.

Runtime and host listening sockets are forbidden. An explicitly approved
outbound call to an external loopback llama.cpp API is permitted. Test-only SSE
servers belong to the e2e harness, not the distributed core.

## Open decisions and completion conditions

The additional Opus query exhausted its service quota. The user then authorized
Astra to answer the outstanding questions, and later requested high effort. Its decisions require
explicit port intersections, invalid legacy signature quarantine, cumulative
Flow scopes, a versioned lossless SQL contract and transactional terminal-audit
outboxes with sink deduplication. Follow-ups settle User policy, the versioned
SQL/transaction API, bounded-engine approval, generic descendant budgets,
compiled provenance, allocation ownership and process exclusion. Current native
untrusted SQL does not establish a hard allocation bound and must fail closed
before preparation. The SQLite WAL coherence/recovery protocol, full ABI path
coverage and runtime proof still require implementation and verification. See
[astra-answers.md](astra-answers.md); do not widen permissions or claim SQL parity
to hide unfinished implementation. SWE-2 max reviews continue.

The legacy ledger, exhaustive migration inventory and final completion gate must
all pass before Python runtime or PackVM deletion. New Harness/Echo examples do
not prove parity for legacy features. Native paths, Windows directory admission,
mobile/P2P transport, legacy workspace migration, complete mediator bridging and
Launcher cleanup remain explicit work. Issue-mandated mechanism removal must
preserve its replacement features; other feature retirement needs human consent.
