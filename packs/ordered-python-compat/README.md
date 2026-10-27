# Ordered Python compatibility for independent pure providers

This pack-owned Rust library implements `ordered-finite-json-v2` and the native
JSON scalar operations used by the original usage-cost and stream-normalizer
providers. It carries no Host, Gate, WIT, authority, process or billing behavior.
The original provider functions and the frozen Python oracle remain unchanged.

`PyValue` preserves object insertion order, exact decimal integers, binary64
floats and integer/float identity. `loads` rejects duplicate *decoded* keys,
including differently escaped spellings of the same key. This follows the
current canonical `ordered_json_v2.loads` callback to `v1._object_pairs`.
`PyValue::object` instead models a Python dict update: a repeated key replaces
the value at its original position.

`loads`/`dumps` retain the protocol limits: 4 MiB UTF-8, 100000 value/key nodes,
and 4300 decimal integer digits excluding the sign. JSON integer `-0` becomes
integer `0`; `-0.0` retains its float sign bit. Decimal underflow remains a finite
float. Parsing and encoding use explicit traversal stacks with no added fixed
nesting policy. Serde JSON's `RawValue` validates number-token grammar without
using its arbitrary-precision private Number-map representation. Its public
string primitives decode Unicode scalar strings and encode UTF-8 JSON strings.
Finite float spelling uses the existing public `tobkiri-canon` LegacyFloat scalar
encoder; this library does not change Core StrictV1, Guest or legacy policies.

`loads_python_json` is a separate explicit entry point for the original tool
bridge's opaque argument string. Its enclosing ordered DATA already proves the
4 MiB text envelope. This parser has no additional node/depth cap, keeps Python's
4300 integer-digit rule, replaces duplicate decoded values at the first key
position, and retains NaN/Infinity and lone surrogates until the finite output
encoder selects an error. It returns `PythonJson`, a separate opaque wrapper;
its private strings and keys use an injective encoding of scalar text, literal
backslashes and lone-surrogate code points. No replacement scalar is inserted,
and the private tree cannot be read as ordinary `PyValue` strings. Paired UTF-16
escapes and literal Unicode scalars compare as the same Python dict key.

`dumps_python_json` serializes native and opaque subtrees in one ordered output
traversal. Parsing syntax and integer-conversion failures precede that traversal;
surrogate versus nonfinite output failures follow the original key/value and
container order. Overwritten values are absent from the final dict, including
their nonfinite numbers or surrogates. Normal `loads`/`dumps` and `PyValue` retain
their existing scalar-string, duplicate, nonfinite, byte and node contracts.
Real fallible reservation failures remain `Resource`. The new opaque behavior
requires independent original-Python native and Wasm evidence before parity
or production acceptance is claimed.

`pytruthy`, `pystr`, `pyrepr`, `pyfloat`, `pyint`, and `pystrip` operate only on the
native JSON-domain variants. Python custom conversion hooks, bytes, tuples,
sets, cycles and arbitrary user objects are outside that value domain.
`pyfloat` preserves the original exception distinction: a huge integer can raise
`CoercionError::Overflow`, whereas the same decimal text can successfully produce
infinity and then be rejected by the provider's finite-number test. `pyint`
truncates finite floats toward zero using their exact binary significand and
exponent, yielding full decimal integers rather than scientific notation.
Numeric strings support Python's Unicode decimal digits, whitespace and legal
between-digit underscores. `pystrip` uses Python's full whitespace predicate;
numeric conversion retains Python's narrower treatment of ASCII U+001C..U+001F.

Unicode printability, whitespace and decimal-digit tables are captured by the
independent `tools/quality/python_provider_compat` oracle under its actual
reference runtime. The checked-in capture used Conda CPython 3.13.9 / Unicode
15.1, separately identified from the unchanged frozen CPython 3.13.5 oracle.
The JSON sidecars retain the actual runtime, executable/module hashes and
generator/source identities; a 3.13.9 capture is not relabeled as 3.13.5 evidence.
The Rust toolchain's own Unicode version does not decide Python string repr.

The original v2 codec is resource bounded, not a guarantee that every permitted
tree fits its interpreter. In particular, Python's native JSON decoder can raise
`RecursionError` with a runtime-dependent stack baseline, while an iterative Rust
decoder can process the same tree. This candidate does **not** invent a fixed
Python recursion threshold or claim parity for those resource-dependent depths.
Fallible codec buffer reservations return `CodecError::Resource` only on a real
allocation failure. Other allocator exhaustion and native guest stack exhaustion
retain the guest resource failure path; they are not silently converted to
malformed JSON. Tree destruction is iterative, including parser error cleanup.
Tree cloning is iterative too. Equality/debug formatting and provider-owned tree
operations can still use native stack space. Consuming `into_object`/`into_array`/`into_string` and
`take` methods move fields without cloning and keep the iterative destructor.

`tests/source_oracle.rs` reads exact bytes and repr strings from the unchanged,
hash-bound frozen CPython oracle rather than constructing expected output with
Rust. Additional provider differential receipts are generated separately from
the original Python adapters and are replayed by the provider crates. No build,
test or oracle generation is performed merely by adding this Source candidate.
