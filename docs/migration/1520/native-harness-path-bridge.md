# Native Harness input and Path bridge candidate

The original Setup methods need actual native-owned inputs in addition to their
DATA arguments. `transport.native-inputs` publishes only the opaque proxy objects
already retained by trusted bootstrap. Its request contains only `format` and
`scope`; it cannot select a name, path, class or binding. The result lists names
and issued references in bootstrap order, with `owned` containing only newly
issued transport holds. Repeated publication uses the same live references.
Release drops a transport hold; it does not close or replace the original input.
Republication after release issues a fresh ID for that same proxy. Partial
registration and output encoding failures roll back newly issued holds.

The guest bootstrap now consumes the fixed `bindings.inventory` import before
loading any original domain module. It validates the invocation Scope, immutable
source-index checksum, complete module-anchor set and closed path rows. The
module-local loader installs each Path constructor and native `__file__` before
executing the unchanged original code object. Its hook remains for lazy imports;
late binding to previously imported original modules is rejected. True native
aliases share one proxy; equal lexical paths with different roles do not.

`NativeBootstrap` now supplies the fixed metadata inventory from an actual
original input owner before `Runtime.init`. Its initial phase rejects every
inspection request; admitting metadata does not implement native filesystem IO.
The opt-in original Launcher adapter retains the actual AppHandle and borrows
the original AppConfig/shutdown state. Its lazy `RuntimeOwner` owns the real
pinned DataRoot, ledger and control, which Core consumes without reopening or
resetting them. This adapter is not yet called from the original product
operation. Each later inspection still needs its actual source capability,
operation policy and physical cleanup. No guest
Scope, source checksum, lexical path or transport ID supplies authority. Missing
native imports do not fall back to ambient guest filesystem access or invented
roots. Whole-component initialization and product integration remain incomplete.

`scripts/wasm/stage_harness_native.py` maps the captured original module closure
and all schema resources into the real import layout, with the actual dispatcher
as the component entrypoint. `build_components.py --harness-native` selects this
independent native-only world for the existing isolated CI producer. It exports
only `runtime` and `transport`, and imports only the fixed native `bindings`
interface. It does not implement the ordinary Pack lifecycle or gain Core/WASI
authority. Frozen three/five-Pack plans are unchanged. The build workflow retains
the exact component, source plan and generated bindings; a successful build is
not proof of Runtime.init, original methods, native IO or GUI operation.

The native-only dependency stage now reuses the genuinely verified Wasm RPDS
extension, locked JSON Schema/packaging wheels and original standard library.
An immutable loader retains all captured original/dependency code and resources,
including namespace packages and late imports. Cryptography is still explicitly
unprovided. Four pure Profile records moved to a private shared module with
unchanged full definitions and legacy public module identities; the original
composition and selector bodies remain unchanged. Dependency separation may retain the
complete original DATA bodies while connecting reached crypto/authority work to
real Rust/Wasm implementations; neither empty classes nor fixed-success adapters
may replace that work. Source transformation evidence and actual runtime/ABI
evidence must remain separate. Initialize and execute the same actual component
through all seventeen original methods before connecting the original product
reachpoint and retiring its Python/VM implementation.

The CI consumer exercises original initialization, the first original method
and actual DATA description under the same physical account. It records peak
charged capacity, remaining fuel and elapsed time. Its separate hostile-component
case rejects inspection during metadata bootstrap, and owner-transfer cases keep
the real root/control/ledger and outstanding reservations. These execution proofs
remain pending: the first dependency-inclusive inner artifact exceeded the
unchanged 32 MiB artifact cap. Lossless immutable source/resource compression
must fit that cap and the same shared 64 MiB/2 billion fuel/30-second/4 MiB
execution limits without dropping code, caching expanded sources or introducing
host filesystem/network fallbacks. A successful artifact build alone remains
insufficient evidence for Runtime.init, all seventeen methods or the product GUI.

`harness_native/native_path.py` implements the guest side of the proposed native
inspection protocol. NativeKit must supply the actual fixed import, native host
path flavor, admitted target bindings and correctly disclosed lexical values.
Constructors, joins, parents, comparisons, ordering and relative paths use the
matching Python PurePath semantics without IO. Expressions retain their native
base binding and lexical derivation. Equal path values do not merge bindings or
permissions; parent/child derivation is not permission to widen a target set.

IO occurs through the existing fixed `inspect` import at each original reached
operation. The native worker must validate actual owner, target, operation,
phase, occurrence and deadline without earlier stat/resolve/prefetch. Relative
`absolute()` obtains live cwd at that reached operation. The optional cwd target
is not an inventory-time snapshot. A module-local Path factory maps only that
pinned module's `__file__` to its actual native resource anchor and must be
installed before top-level original code executes. It is a fixed class facade:
delegated original `isinstance(value, Path)` checks the concrete wrapper type,
without trusting `__class__` or checking/creating authority. The same wrapper is
recognized across module facades; exact type and other unobserved class APIs
are not claimed compatible. Its native role, including
allowed resource parent transitions, requires separate admission.

The OS facade preserves O_NOFOLLOW presence, open flags, each short read and EOF,
the original observed stat fields, and close errors. Rglob starts on first
iteration, preserves native enumeration order, and lets original guest code
consume and sort it. FD and iterator references stay opaque. NativeKit must own
their actual resources through all worker Joins, close completion and teardown,
including cancellation when guest finally blocks cannot run. Encoded bytes,
decoding and ABI copies remain inside the original DATA/output budgets.

The Unix native `write_fd_read` primitive now reads directly into the end of one
prepaid inspection tail and encodes forward in place, including exact Base64
padding and the caller's frame prefix/suffix. It performs one successful raw
read, preserving short reads and each EOF, and retries only EINTR after checking
the original activity/work scope. The maximum frame is charged before IO; no
second raw/encoded Vec is allocated. New checkpoints borrow only the existing
scope, cannot escape the callback or create owners, and latch the first failure.
Kernel checks also precede execution and successful private tail commit even
when a producer ignores its checkpoint. A failure cannot publish partial raw
data or turn into a successful/domain result. Output rollback never rewinds or
replays the FD read. Blocking IO still retains its actual FD through the real
worker Join; checkpoints do not promise to interrupt a blocked syscall.

This primitive takes the native caller's actual File, not a guest descriptor or
path. Actual source/FD/phase admission, the complete descriptor registry and
Python signal-exception compatibility remain separate requirements. Portable
tests exercise real files plus the real kernel writer with an explicitly
injected root lease; they are not native root/admission or whole GUI proof.

The native `write_fd_stat` producer prepays its complete framed maximum before
the original `File::metadata` call. It writes the five observed integer fields
directly into that tail, including signed nanoseconds computed without a float.
Original syscall errors remain typed and undisclosed; execution failure wins
over a domain result and cannot publish a partial frame.

`NativeFdProtocol` validates the existing guest FD request format and dispatches
read/fstat to the caller's actual File. Its four hex Scope values are only DATA
correlation: it additionally matches genuine invocation custody before parsing
with the original allocation owner. The native-issued FD binding must match,
derivation steps must be empty, and extra fields, foreign scopes, negative read
sizes and unsupported operations fail before file access. Rejections are sticky
kernel execution failures even if a callback ignores the returned error. Read
bytes retain their original short-read/EOF behavior and original envelope.

This adapter supplies no source admission, registry, open or close operation.
The actual source controller must retain and validate source, target, phase and
occurrence before dispatch. Syscall errors still require the controller's proven
exception-disclosure policy; rejecting an unsupported negative request is not
proof of Python ValueError restoration. The adapter is not wired into production
bootstrap, and no source permission follows from its constructor or Scope.

Only explicit returned domain-error DATA may use the dedicated original
exception decoder. Resource, cancellation and panic failures are not converted
to domain success/errors. Unconnected error disclosure, unbound IO paths,
unsupported observations, corrupt results and foreign contexts fail explicitly;
there is no ambient filesystem fallback.

The portable tests verify publication, lexical expressions, protocol order,
short-read/EOF handling, iterator cleanup and error propagation with named
fixtures. They do not prove native authority, physical resource cleanup or
whole Setup parity. Actual native target admission, original module preamble
installation, complete delegated Path/OS API audit, whole component production
and native oracle comparisons remain required. The existing product default,
Setup/approval windows, settings and conversations are unchanged.

The guest adapter now also supports lexical `name`, `suffix`, `suffixes`,
`with_name` and `with_suffix`, preserving its original source binding and
PurePath validation. `iterdir` requests original enumeration at the method call
(the pinned Python 3.13 timing), then consumes entries in native order. `glob`
and `rglob` request iteration when first consumed. Each live iterator closes on
exhaustion, iteration failure or explicit generator close after iteration starts;
native invocation teardown must also release any retained unconsumed inventory.
Native enumeration, filtering, error timing and exact glob-pattern validation
still require the native implementation/oracle; protocol fixtures do not prove
them. Guest consumers retain their original sorting/cache behavior.

Direct `fspath`-to-`open` provenance, `fdopen` and buffered file-handle
ownership/read/seek/close semantics, the original monotonic clock domain and
fixed native codesign execution remain unimplemented. In particular buffered
`read(n)` cannot be replaced by a single raw `os.read(n)`, and a path string
cannot recover authority by equality.

## Existing resolver handoff

`SignedApplicationResolver::resolve_sources` now retains the actual successful
resolver result, the application/bundle/application-Pack root locals and the
verified bundle-lock pins in a private-construction `NativeApplicationSources`.
It moves those existing values at the original return point, without cloning
them, re-resolving paths or repeating validation/IO. The existing `resolve`
entrypoint returns the same `ApplicationAuthority` from that one resolver pass.
Native root roles remain distinct even when their path values are equal.

This is the later resolved-application handoff. It must not become a prerequisite
for earlier Setup preview/source display, or precompute those original guest
checks natively. Earlier inputs require their own actual Config/Tauri selection
role at the original point. Native invocation phase checks must retain the real
context; a caller phase name or this holder's existence is insufficient.

Root and pin getters borrow the retained selection without IO. The selection
does not prove that an inode/content remains unchanged, hold an FD, grant file
permission or provide NativeSetup allocation funding. Native target admission,
funded invocation retention, original-point observations and actual FD custody
remain required. The two real-resolver tests cover owned roots/pins after rename
and original rejection behavior; they run in Launcher CI, not locally.

## Earlier original Host inputs

`native_harness_host_inputs::with_original_host_inputs` borrows the existing
`AppConfig` from the actual retained Tauri `AppHandle`, separately from the later
signed-application resolver. Its callback receives the existing selected runtime,
working-directory and optional development-workspace roots. It neither clones a
PathBuf nor repeats resource-dir resolution, fallback, stat or validation. Equal
path values preserve distinct source roles and original backing allocations.
The higher-ranked callback prevents a root borrow escaping its actual owner;
the original shutdown state is checked before use and before returning a result.

The native caller must hold that actual AppHandle through its original managed
worker/physical Join, check the real invocation/target/phase/disclosure and fund
any new captures, proxies, derived paths or output before allocation. Reusing an
actual preexisting Host allocation keeps its original accounting treatment; it
does not exempt newly allocated nativeKit data. These root views are selection,
not filesystem permission or current artifact verification. The API introduces
no new startup prerequisite and is not yet wired into the complete native import
controller or module bootstrap. Its source tests cover borrowing/retirement;
they do not prove the full nativeKit or GUI migration.
