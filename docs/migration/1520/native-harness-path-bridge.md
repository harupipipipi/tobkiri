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

The delegated source audit also requires `iterdir`/`name`, schema resource
`with_name`/`suffix`/`glob`, direct `fspath`-to-`open` provenance, `fdopen` and
buffered file-handle ownership/read/seek/close semantics, the original monotonic
clock domain and fixed native codesign execution. These additional adapters are
not implemented here. In particular buffered `read(n)` cannot be replaced by a
single raw `os.read(n)`, and a path string cannot recover authority by equality.

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
