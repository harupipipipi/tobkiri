# Finalizer `_normalize_result` usage source observation

`generate.py` creates a fresh, independent source observation of the unchanged
`original_gateway_finalizer._normalize_result` function. It takes the Candidate
and other fields from the first `generate` record in
`packs/local.ai-gateway-finalizer-python/source-cases.json`, then changes or
omits only `value.usage`. It verifies the prepared extraction with
`prepare_gateway_finalizer.verify` and uses `observe_gateway_finalizer.runtime()`
and `source_modules()` to load the current Python sources. It does not call the
older observer's frozen `observe()` or `source_bindings()` paths, reuse their
expected outputs, or invoke a dependency, Host, Core, WIT binding, or Wasm
component. The historical oracle remains unchanged and is pinned to its own
CPython 3.13.5 evidence.

The hand-authored inputs probe falsey and absent usage, a string (`"ab"`),
two-character non-BMP Unicode string pair row (`["😀💥"]`), array pairs,
`True`/`1`/`1.0`/`None` keys, duplicate keys, both two-key object insertion
orders, invalid pair lengths, and unhashable keys. The generator contains no
expected results. CPython produces each result or source exception. Result
values use `io.tobkiri.python-value-tree.v1`: dictionaries are represented as
ordered key/value item pairs with tagged Python types, so non-string keys and
Python's equal-key overwrite behavior remain visible. Each successful result
is also passed to the captured `ordered_json_v2.dumps`; its exact bytes are
stored when encoding succeeds, and the observed exception type/message are
stored when it fails.

Run with CPython 3.13.x, Unicode database 15.1.0, and the unchanged
`int_max_str_digits=4300` setting. The receipt records the actual Python
micro-version, full `sys.version_info`, and recursion limit. From the
repository root, choose a new absolute output path:

```sh
python3.13 tools/quality/python_finalizer_normalize_compat/generate.py \
  --output-dir /absolute/path/to/new-private-receipt
```

The generator writes a fresh `0700` directory, a `0700` sidecar directory, and
`0600` receipt/input/output files. The receipt hashes the current source closure,
the verified extraction provenance, generator, case definitions, exact input
bytes, tagged Python outputs, and any successful ordered-v2 output bytes.
Limits are 32 cases, 256 KiB per input, 2 MiB total input, 512 KiB per output,
4 MiB total outputs, 16 MiB per source, 32 MiB total source, and 1 MiB for the
receipt.

This is a narrow, newly generated source observation of Python dictionary
coercion. It does not establish dependency-call behavior, Rust parity, Core
acceptance, Wasm behavior, Host behavior, or production readiness. It does not
modify the original ten frozen cases, their expected results, or the existing
source observer.
