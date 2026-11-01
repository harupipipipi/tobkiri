# Rust tool bridge candidate

This additive candidate translates the original pure tool-intent normalizer and
its **v2** adapter. It reuses the existing
`local:ai-tool-bridge-ordered-python/bridge@2.0.0` WIT ABI by referencing its exact
WIT file together with canonical Core WIT. Freestanding methods are static and
the component export is enabled only for wasm32. Runtime-info names Rust and
`local.ai-tool-bridge-rust`; it never reports a Python interpreter version.

The three registered operation identities map to the original normalize call.
The implementation preserves request-id coercion/strip, function/name fallback,
arguments **presence** rather than truthiness, original list indexes, and all
eight output fields in their original order. Non-mapping list entries are
skipped during the original comprehension and rejected by its final count check
after later mappings have been normalized. No tool is executed. Nested
`approved`, `authority_granted` and similar argument keys remain caller DATA;
the normalizer's outer flags retain the original false/unrequested values.

The shared `ordered-python-compat` codec supplies ordered v2 DATA and Python
coercions. Its explicit opaque Python-JSON entry point retains duplicate-last-
value at the first key position and holds NaN/Infinity and lone surrogates until
final output encoding. It imposes no new opaque node/depth cap. The enclosing
input string already proves the 4 MiB text envelope. Python's actual 4300-digit integer rule
still applies. Regular ordered v2 input rules remain unchanged.

Original closed v2 errors are retained: INVALID with
`outside ordered-finite-json-v2 domain`; INTERNAL with
`provider output contains a non-finite number`; and the resource path with
LIMIT_EXCEEDED / `ordered tool bridge resource limit exceeded`. Fallible
allocation failures follow the resource path without inventing a logical depth
or node limit for opaque JSON. Tree expansion and runtime resource requirements
can differ between Python and Rust and require separate actual evidence.

There is an explicit **production hold** pending actual evidence. Opaque Python
JSON arguments are retained in a separate private `PythonJson` tree, including
lone-surrogate strings and keys. The original normalizer never applies str/repr
to this subtree. The mixed native/opaque output encoder visits it only after
all intents have normalized, preserving the original choice between a surrogate
INVALID failure and a nonfinite INTERNAL failure in output traversal order.
Decoded duplicate keys replace only the value at the first key position;
overwritten bad values disappear before encoding. The ordinary `PyValue` domain
and ordered-v2 request semantics are unchanged. No source review establishes
parity: every independently captured original-Python case remains mandatory.

The unchanged 33-case CPython 3.13.5 / Unicode 15.1 v1 oracle is exercised by a
separate test-only v1 transport fixture (safe integers and depth 64). Its 12
successful, 12 invalid and 9 excluded rows remain their historical categories;
the excluded rows are not counted as successful semantic parity. This fixture
does not redefine the v2 component's behavior or its expectations.

After Root registers the new package in the audit workspace, the v2 test
consumes an independently generated original-Python receipt:

```sh
TOBKIRI_TOOL_BRIDGE_ORACLE_RECEIPT=/absolute/path/receipt.json \
TOBKIRI_TOOL_BRIDGE_ACTUAL_DIR=/absolute/private/new-actual-directory \
cargo test --manifest-path packs/Cargo.toml \
  -p tobkiri-pack-ai-tool-bridge-rust --test oracle_receipt -- --test-threads=1
```

All 81 v2 cases are required and executed. Exact result bytes or fixed errors
and hashes are saved in a fresh private directory before any final nonzero
comparison. No case is skipped or accepted merely because a failure category
is convenient. Without the receipt environment variable this differential
is explicitly unexecuted. Original-source native fixtures, real Wasm execution,
Core owner/Gate/audit acceptance and composition are separate evidence. Source
formatting or a native test pass does not activate a Pack or establish them.

This directory does not change original Python, WIT, frozen expectations,
workspace members, locks, existing targets, signatures, Profile or the 64 MiB
component cap. Root owns adoption, builds, oracle generation and actual replay.
