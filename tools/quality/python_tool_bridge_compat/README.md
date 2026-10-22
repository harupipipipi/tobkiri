# Tool Bridge v2 source oracle

`generate.py` creates a new `io.tobkiri.python-provider-compat.v1` receipt for
the unchanged Tool Bridge v2 Python sources. It records fresh CPython results
and closed `app_v2.py` WIT error mappings; it does not import `app_v2.py`, WIT
bindings, or the host factory, and it does not run Rust or Wasm. Use CPython
3.13 with Unicode database 15.1.0 and the unchanged 4,300-digit limit: the
Rust receipt replay checks those runtime facts before accepting the receipt.

The 33 cases from the archived v1 oracle contribute only their frozen operation
IDs and raw `input_json` text. `oracle.json`, `oracle.freeze.json`, the freeze
script, and source provenance are hash-checked before those inputs are used.
The old `original` and `transport` expectations are never used for v2. The
additional hand-authored cases cover v2 aliases and operation rejection,
ordered opaque duplicate keys, negative zero and underflow, 4,300/4,301-digit
integers, non-finite values, malformed/non-object inputs, function/name/argument
fallback rules, mixed intent order, nested truthy identifier representations,
100,001 collapsible opaque duplicate keys, and surrogate/NaN serialization
order. No surrogate case is filtered or assigned a Rust-derived expectation.

Before compiling captured source, the generator captures its full bounded
source closure using no-follow regular-file reads and hashes the exact bytes.
The receipt binds the canonical `bridge.py` extraction, both adapters and JSON
codecs, the v2 app and WIT sources, frozen input records, and the generator and
case definitions. Only `original_bridge.py`, `caller_adapter.py` (for its
registered operation IDs), `caller_adapter_v2.py`, and the captured codecs are
executed. `app_v2.py` is parsed as syntax to obtain its literal closed error
mapping; no WIT app import or authority operation occurs.

The receipt reports the actual CPython version, Unicode database version,
integer digit limit, recursion limit, and hashes of the running interpreter and
Unicode module where available. It does not change recursion or integer limits.
An unsupported source exception aborts generation; it is never converted into
a guessed expected error. Published error records contain only the fixed WIT
code and message from `app_v2.py`, never exception details.

Use a fresh absolute output path:

```text
python3.13 tools/quality/python_tool_bridge_compat/generate.py \
  --output-dir /absolute/path/to/new-private-receipt
```

The v2 differential consumer is separate from receipt generation. It runs every
case and writes exact outcomes, including unsupported surrogate mismatches,
into a fresh actuals directory before returning a mismatch status:

```sh
TOBKIRI_TOOL_BRIDGE_ORACLE_RECEIPT=/absolute/path/to/new-private-receipt/receipt.json \
TOBKIRI_TOOL_BRIDGE_ACTUAL_DIR=/absolute/path/to/new-private-actuals \
cargo test --manifest-path packs/Cargo.toml \
  -p tobkiri-pack-ai-tool-bridge-rust --test oracle_receipt -- --test-threads=1
```

The generator refuses an existing output directory. It creates a `0700`
directory tree and writes `0600` receipt and sidecar files. Limits are 128
cases, 1,024 UTF-8 bytes per operation, 4 MiB plus 128 bytes per raw input and
16 MiB total input, 1 MiB per successful output and 8 MiB total output, 8 MiB
per captured source and 32 MiB total source, and 1 MiB for the receipt. Runtime
provenance hashes at most 512 MiB for the Python executable and 64 MiB for the
Unicode module. These are oracle capture bounds, not claims about native Pack
admission.

This is source-observation evidence only. A receipt does not establish Rust
parity, Wasm behavior, Core/Gate acceptance, setup completion, approval,
activation, or production readiness. A candidate mismatch, including a
surrogate representation gap, must remain visible and blocks parity claims.
