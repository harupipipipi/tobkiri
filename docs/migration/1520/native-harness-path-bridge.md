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
installed before top-level original code executes. Its native role, including
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
