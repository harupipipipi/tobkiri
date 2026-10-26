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
original input owner before `Runtime.init`. Its initial phase rejects filesystem
inspection; only a closed, bounded failed-init observation and seven fixed
initialization checkpoints may use the existing fixed import. Checkpoints use
only a fixed stage and monotonic sequence. Before Scope decoding, the actual
native Store/invocation and `INITIALIZING` phase still validate the import;
guest-supplied identity does not authenticate it. One fixed-size native trace
retains actual host phase, import entries separately from validation counts,
last rejection and last accepted checkpoint. It retains no arbitrary log list.
Admitting inventory or a checkpoint does not implement native filesystem IO.
The opt-in original Launcher adapter retains the actual AppHandle and borrows
the original AppConfig/shutdown state. Its lazy `RuntimeOwner` owns the real
pinned DataRoot, ledger and control, which Core consumes without reopening or
resetting them. This adapter is not yet called from the original product
operation. Each later inspection still needs its actual source capability,
operation policy and physical cleanup. No guest
Scope, source checksum, lexical path or transport ID supplies authority. Missing
native imports do not fall back to ambient guest filesystem access or invented
roots. Whole-component initialization and product integration remain incomplete.

The 8f72 actual component (24,837,771 bytes, SHA256
`56146382ed1c42b19f720240e3bf0b72a37ad3339a5c7b7ca9e67269d0923604`)
passed admission and prepared in 16,514,529 microseconds inside the original
30-second operation. Actual `Runtime.init` then failed in `PyErr.print` through
the terminal `isatty` stub, without delivering a pre-print diagnostic. Its
original Python exception remains unknown. Native failure records now retain
the actual trace, fuel/capacity observations when available, original deadline,
elapsed time and original outer error even when the diagnostic is absent.
The seven checkpoints localize progress; they do not identify an exception or
prove initialization success. No stdio/WASI access, exception substitution or
resource-limit increase is introduced. The producer's outer `PyErr.print`
boundary currently has no proven low-dependency native observation hook; this
change does not claim to have instrumented that boundary.

The af0b checkpoint baseline subsequently failed with **zero inventory imports,
zero inspect imports and zero checkpoints**. The native record kept the actual
outer trap and 1,980,212,518 fuel remaining, with no recorded deadline overflow.
Preparation used 29,533,618 microseconds; neither this timing nor the absent
checkpoints identifies the original Python exception. Its metadata5/policy6/
owner2 cases all passed, separately from the actual init failure.

The dependency entry wrapper sealed its captured Finder before
`wit_world.imports.bindings` had been imported. Checkpoints, failure reporting
and bootstrap all import that same fixed module later, while the sealed Finder
refuses any uncaptured nonbuiltin/nonfrozen module. The wrapper now preloads
only that interface before the existing original-module guard and seal. It
checks the actual `/world` module and Python function layout against the
pinned producer's runtime shims, including the `componentize_py_runtime`
module identity and `call_import` bodies. This performs no host operation.
The later imports retrieve those same objects; unknown modules remain denied.
Path/layout checks establish build provenance only and grant no operation.

The retained `generated-bindings` artifact is the **IDE stub output** of the
separate `bindings` command. The actual componentize pass generates its runtime
bindings separately with `generate_code(false)`, mounts them at `/world` and
preinitializes the exported Runtime/Transport objects from the wrapper. A
retained IDE source hash does not prove the actual runtime callable. The new
preload checks run during that actual producer pre-init, so a stub or foreign
binding fails the component build. Source fixtures cover no native calls during
preload, stable module/callable identity after seal, unknown-module refusal and
stub rejection at the same path. The actual 772d789 consumer (run
`40416195917`, job `121900800880`) then reached one validated inventory import
and four validated checkpoints, ending at `factory-entering`. It failed at the
original deadline, with 1,621,949,300 fuel remaining, no inspection rejection
and no unreachable trap. Cold preparation had consumed 28,468,923 microseconds;
the full failed operation lasted 30,004,288 microseconds. The parser position
in that deadline backtrace does not establish the dominant cost. This confirms
forward progress through the fixed bindings, not successful initialization or
the identity of the earlier hidden exception. No outer hook or pre-init
operation probe is added.

Cold preparation now has one shared implementation for both the opt-in actual
Launcher adapter and the actual CI consumer. The caller begins the original
operation before Engine creation, artifact reading and compilation. The helper
retains that same source owner, invocation, account and absolute deadline through
initialization. It charges source storage before allocating the read buffer,
verifies the actual bytes against the selected digest, safely compiles them and
drops the allocation before releasing its reservations. A File or digest grants
no source or effect authority. The 32 MiB source, 64 MiB shared payload, 2 billion
fuel, 4 MiB envelope and original 30 seconds remain unchanged. Source accounting
does not claim to measure the compiler's entire native heap or compiled metadata.

The generic Engine accepts an explicit compiler optimization setting. Existing
callers retain `Speed`; only this common native composition selects `None` to
evaluate cold compilation cost. There is no CI-only compiler choice, precompile,
persistent cache, unsafe deserialization or replacement execution interval.
Two required Mac preparation tests cover rejected sources, original deadline,
reservation release and retained source custody. Actual whole-component
preparation, initialization and methods must still pass within the same limits;
reduced compile time alone is not acceptance. The change remains unverified
until that actual consumer runs, and the original production route is still
unconnected.

Production singleton ownership also remains unresolved. The original callees
retain issued-origin/class/exception identity, thread/ContextVar custody,
ActivationStore retry state and shared flight/waiter synchronization. Fresh
Stores, a serial mutex, or whole-Store cancellation do not prove equivalence.
Before a resident owner is implemented, CI must establish the actual resolved
and compiled Wasmtime features, generated ABI, CPython thread/context backend,
same-state concurrent progress and individual operation termination/state
safety. The native consumer retains both the resolved feature graph and the
actual compiler fingerprints. These observations are not concurrency tests.
The pinned historical producer's authenticated source manifest was verified,
but its receipts and artifacts contain no generated `pyconfig.h`. The next
producer build retains the actual generated and installed headers and small
thread/context sources alongside their existing source/recipe receipts; it
cannot retroactively prove the old binary's backend selection. No resident
wrapper or product default is enabled on the strength of these source facts.

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
through all nineteen currently selected original operations (the original
seventeen plus both preexisting HostPort root getters). Remaining original
methods and the complete product flow must also pass before retiring Python/VM.

The compression predecessor `84abc32b` actually built the complete component:
24,828,709 bytes, SHA256
`b8aff392402f011fef7d76eae3228076f3d6a26c31ef51c21cac8d9c724922b7`,
run `40382460471`, build job `121782008368`. It fits the unchanged 32 MiB
artifact cap. The same run's actual native consumer `121782981068` failed
`Runtime.init`: a secondary `isatty` trap during Python exception printing hid
the original exception. Initialization, methods and the product flow are not
proved by that build.

The native-only dispatcher now retains the same actual factory/registry
exception, records up to 4096 UTF-8 bytes through the existing fixed inspection
owner, then bare re-raises it. Exact builtin descriptors, scalar arguments and
bounded traceback positions may be recorded; display hooks, unknown-class
attributes and frame locals are never inspected. Recording failure cannot
replace the original failure. Native execution keeps the original outer error,
the separate observation and actual Store/resource counters. No new stdio/WASI,
domain-error conversion, guest replay or execution allowance is introduced.
The new diagnostic still needs the same actual component/consumer CI proof.

After successful initialization and the original pure Profile-id method, the
consumer executes both original root getters as component-level unit operations
and checks opaque proxy descriptions and actual module resolution observations.
This is not the production development/packaged branch or final binding check.
Five real native metadata fixture tests and six hostile/cancellation tests run
before that consumer. Independent execution/owner evidence is collected even
when another test fails, only after the real platform and artifact-input steps
succeed; every failed required test still fails the job. The same shared
64 MiB/2 billion fuel/absolute 30 seconds/
4 MiB envelope applies to all of these operations within an invocation.

The actual consumer now starts its original invocation before engine creation,
artifact reading and component compilation, matching the product preparation's
ordering. It retains the original deadline through initialization/methods and
charges the actual source Vec to that same payload account until deallocation,
alongside its existing code-admission control reservation. It records actual
preparation time/peak and rejects an already expired deadline before init. Older
consumer measurements that compiled first do not prove this full time boundary.
This does not claim the compiler's entire heap/RSS is covered by the source
buffer reservation; complete compiler resource accounting and shared actual
product preparation remain separate integration requirements.

The original runtime registration is process-singleton by object identity.
The candidate factory now borrows that module-owned singleton instead of
constructing a second object. It does not call the public installer early;
registration must be connected at the original production-composition point,
before any original require-based request entrypoint. Candidate class-method
unit execution is not proof of that registered production Port. Isolated tests
use the unchanged declaration/registration bodies to check unavailable-before-
install, identity, repeated installation and refusal to replace another object;
they do not import the SDK preambles or prove Wasm/whole-application lifetimes.
Catalogs are rebuilt for the separate GET review and POST activation; GUI wait
holds presentation/confirmation DATA outside those HTTP operations. A stable
native facade alone does not prove that recreating the original singleton in a
fresh Store preserves its instance/module/class state, aliases, callbacks or
initialization effects. That full state/lifetime/concurrency audit is required
before choosing production Store lifetimes. A single HTTP operation cannot
reset its deadline by retrying, recreating its Store or nesting another call.

Root getters admit only the explicit native composition policy for the exact
original module name and relative source file, `resolve(false)`, unchanged
Scope and an empty derivation. Inventory/path/hash/FD existence alone supplies
no authority. The actual reached operation uses retained descriptor-relative
metadata and validates parent/name identities. Internal links and non-strict
missing suffixes have native fixtures; an external SDK relation, non-UTF-8 link
or full-path syscall boundary remains unsupported outer execution failure.
Those refusals are not claimed as complete original Path compatibility.
Vector replacement prepays both simultaneous allocations. Dropping a suspended
getter closes its admission and retires the same IO owner through physical Join.
Module resolution and opaque getter success do not yet prove the final parents
target or subsequent resource access; no path-disclosure API supplies that proof.

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
