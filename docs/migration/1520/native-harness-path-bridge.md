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

The current original-runtime bootstrap has no admitted native input inventory.
It therefore reports publication as unavailable rather than returning a fake
root. Neither an issued ID nor its serialized Scope grants native authority.

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
