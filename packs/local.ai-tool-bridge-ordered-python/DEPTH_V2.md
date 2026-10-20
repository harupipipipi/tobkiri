# Iterative depth handling in the unshipped v2 profile

The same unshipped `ordered-finite-json-v2` source candidate now combines bounded
integers with iterative traversal. It is not a new v3 alternative. The preserved
v1 codec, oracle, component, receipt, source plan and active provider are unchanged.

## Domain and resource boundary

V2 retains the 4 MiB UTF-8 document budget and 100,000-node budget, including object
keys and every occurrence of shared children. It removes the independent
depth-64 restriction. A path of depth D requires at least D+1 counted nodes, so
the node budget already implies root-zero depth at most 99,999. This is a derived
upper bound, not a promise that the runtime accepts documents at that depth.

The encoder and validation walk use one explicit frame per active container.
They consume children one at a time rather than materializing all pending
children. Active identities detect cycles; repeated, noncyclic shared children
are allowed and counted again. The scanner is iterative and counts quoted
strings as opaque values. Bytes and nodes cap work and auxiliary structures;
they do not establish execution within any guest fuel, memory or time budget.

The standard library still validates JSON grammar and builds the decoded tree.
Its real `RecursionError` is preserved, not rewritten as malformed JSON.
Consequently an iteratively encoded, in-budget value can still encounter a
decoder resource failure. There is no universal accepted nesting-depth claim and
no change to `sys.setrecursionlimit` or the interpreter's integer-digit limit.
A complete iterative JSON parser is deliberately outside this bounded revision.

The original normalizer alone parses opaque argument JSON. Its actual integer
conversion and recursion failures still propagate through the source adapter.
No pre-parsing, truncation, wrapping, flattening or replacement of its values is
introduced. Exact native types, insertion order, finite binary64, negative zero,
Unicode scalar strings, outer duplicate-key rejection, and the 4300-digit integer
bound are retained.

## Finite output and typed errors

The encoder raises `NonFiniteJSONError`, a `ValueError`-family codec exception,
only when traversal reaches an exact nonfinite float after its node visit.
This allows the adapter to distinguish nonfinite normalized output without a
second, potentially divergent tree walk. Earlier byte, node, cycle or type
failures retain their ordinary precedence. Native decoder hooks still reject
nonfinite request JSON as ordinary codec input errors.

The source adapter does not emulate the old Host's audit serializer. Adding a
`json.dumps` feasibility gate would duplicate serialization and sorting and
reintroduce a build-specific recursive resource ceiling. The migration does not
claim preservation of all historical Host/HTTP resource failures. Existing
canonical/seal/digest byte compatibility is a separate requirement, not changed
by this transport revision.

A future v2 component boundary must map actual recursion/resource failures to
the existing WIT `limit-exceeded` code with a fixed public message, independently
of invalid input and the reviewed nonfinite-provider failure mapping. This
document does not claim that the new component entrypoint is built or wired.
Wasm traps and exhaustion still require their real Core handling and evidence.

## Proof and remaining integration

The depth tests compare all three immutable frozen depth inputs against actual
original normalization and independently serialized output bytes. They also
exercise 256- and 1024-level structures, combined bigint/depth values, node and
byte boundaries, cycles, shared children, malformed JSON, opaque strings,
chunk-crossing escapes and unchanged v1 depth behavior. Native decoder tests at
the broad depths explicitly require the exact CPython 3.13.5 / Unicode 15.1.0
reference; they do not infer guest capacity from the native interpreter.
Resource-error propagation tests inject the exception without probing a stack
limit or changing one.

Both the inner ordered text and the unchanged CoreStrictV1 outer envelope must
fit their own 4 MiB byte limits. Quoting can exceed the outer limit even when the
inner document fits. The payload remains an opaque string, never a Core object.

Fresh combined source observations must bind the final codec and adapter bytes.
The next real component needs the reviewed v2 WIT/Pack route, caller, entrypoint
and staging plan, followed by a producer receipt, actual guest replay under the
existing runtime bounds, and native owner/consumer permission/audit acceptance.
Expected outputs remain outside the guest. None of those execution or activation
gates is established by pure-Python source tests.
