# Original Python provider compatibility oracle

This source-only generator records finite CPython observations for the original
usage-cost and stream-normalizer providers. It invokes the existing unchanged
Python adapter and extracted provider function sources with the shared
`ordered_json_v2` codec. The canonical runtime source, extracted function,
adapter, `app.py` error mapping, codec, extraction provenance, Core WIT error
enum, generator and static case definitions are captured and hashed before the
provider modules are loaded. Captured bytes are compiled in memory, so the hash
describes the source that is invoked.

The generator does not import or construct WIT classes, execute `app.py`, run
Rust/Wasm, change Python's integer or recursion limits, or create expected bytes
from either Rust implementation. Fixed WIT error code/message results are read
declaratively from the hash-bound `app.py` handler source. An exception without
one of those closed mappings aborts receipt creation instead of becoming an
invented expected result.

## Generate a private receipt

Run from the repository root with the intended actual CPython interpreter. The
output directory is required, absolute, new, and below a physical (non-symlink)
parent directory. Existing paths are rejected; files are created with mode
`0600` and directories with mode `0700`.

```sh
python tools/quality/python_provider_compat/generate.py \
  --output-dir /absolute/private/path/python-provider-compat-NEW \
  --unicode-tables \
  --scalar-vectors
```

Use a fresh private directory for every invocation. The tool labels the exact
interpreter and Unicode database it observes. `--unicode-tables` requires the
actual CPython Unicode database version `15.1.0`; it does not relabel another
runtime as CPython 3.13.5 or 3.13.9. The interpreter's current integer-digit and
recursion limits are recorded without modification.

The generated `receipt.json` uses schema
`io.tobkiri.python-provider-compat.v1`. Its `sources` list contains repository-
relative paths, byte counts and SHA-256 digests. The top-level
`source_manifest_sha256` hashes that canonical source list, and each case binds
the same value. Cases are a flat `cases` array. A case records its provider,
operation, raw input descriptor, and one of:

```json
{"kind":"result","output":{"path":"cases/usage-cost/example.output",
"encoding":"utf-8","bytes":12,"sha256":"..."}}
```

or a closed error such as:

```json
{"kind":"error","code":"invalid","message":"invalid usage cost request"}
```

Exact UTF-8 payload bytes are stored under `cases/<provider>/<id>.input`.
Successful adapter results are stored byte-for-byte under the matching `.output`
path. Both descriptors contain byte counts and SHA-256 digests. WIT error codes
use their Core spellings (`invalid`, `internal`, `limit-exceeded`); error text is
the fixed source `app.py` text, never an arbitrary exception message.

The static corpus covers both provider alias sets, malformed and duplicate-key
inputs, ordered nested DATA, original Python numeric and string coercions,
terminal stream event behavior, original failure mapping, integer-digit and
native-recursion edges, and bounded codec input/output limits. It is an additive
source oracle. It does not alter or replace the 33 frozen tool-bridge vectors or
the 10 frozen finalizer vectors, and its creation alone proves no Rust parity.

The generator bounds case count, input and output byte totals, receipt size,
and each raw successful output to 1 MiB. Inputs used to exercise the v2 byte
boundary are slightly larger than 4 MiB but remain explicitly capped. If an
unexpected source exception, oversized successful result, source/provenance
mismatch, or unsafe path occurs, no receipt is written. A filesystem failure
after output creation may leave an incomplete private directory; use a new path
for a retry rather than reusing or overwriting it.

## Optional Unicode compatibility table

With `--unicode-tables`, `unicode-table.json` is an ASCII JSON artifact using
schema `io.tobkiri.python-unicode-compat.v1`. It scans every Unicode scalar
value in U+0000..U+10FFFF (surrogates excluded) using the actual interpreter's
`str.isprintable`, `str.isspace`, and `unicodedata.decimal`. It stores inclusive
nonprintable ranges, whitespace codepoints and decimal-zero codepoints, plus
the actual Python executable hash, `unicodedata` module hash when that module is
a regular file, actual CPython/Unicode identity, and the generator source hash.
The table is separately hash-bound in `receipt.json`. It is compatibility data
for later Rust work, not evidence that Rust agrees with Python.

## Optional scalar builtin observations

With `--scalar-vectors`, `scalar-vectors.json` uses schema
`io.tobkiri.python-scalar-compat.v1`. Its finite static inputs are decoded by
the captured original `ordered_json_v2` module, then probed with the actual
CPython builtins `float`, `int`, `str`, `repr`, `bool`, and `str(value).strip()`.
Each vector includes its raw JSON text and UTF-8 byte count/hash, decoded type,
and exact result data or exception class/message. Float observations include
`float.hex()`, `repr`, `str`, and finiteness; integer observations include the
exact decimal result. The sidecar records its own runtime identity, Python
executable hash, `unicodedata` module hash when available, a canonical runtime
identity hash, and the same source-manifest hash as `receipt.json`; the receipt
binds its path, byte count, and SHA-256. This describes builtin conversion behavior only. In
particular, nonfinite builtin results are explicitly outside DATA-codec parity
scope. The vectors do not alter provider inputs or expectations.
