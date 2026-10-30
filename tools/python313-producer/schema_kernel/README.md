# Original Python-object schema port: primitive stage

This is an incomplete, private Rust/PyO3 building block. It is not a validator
or product replacement. No schema handler or error graph is implemented yet.
The original Python product validator remains unchanged.

The previous jsonschema 0.60.2 candidate is rejected. Actual CI 41058189370
built/imported the extension and entered Rust through the real NativeData
consumer, but registry initialization called unavailable WASI randomness and
trapped. Its separate serde schema/error-reference and ordering gaps also
prevented parity. The 64 rich-error comparisons were NOT run. No fixed entropy,
preinitialized registry or extra authority was introduced to force it to pass.

## Current primitives

- `retain_objects(schema, instance)` returns a Python dictionary retaining the
  actual two objects, without serialization, copies or path reconstruction.
- `is_original_json_type(instance, kind)` implements default JSON primitive type
  predicates from pinned jsonschema 4.26.0 / Draft 2020-12: seven JSON types,
  bool excluded from integer/number, integral floats accepted as integer, and
  the actual Python `numbers.Number` ABC. It converts no bigint to a float.
  Unknown names raise ValueError in this private helper; this is not the full
  original TypeChecker API or its UndefinedTypeCheck exception contract.
- Module markers are `production_enabled=False`, `implemented_handler_count=0`.

The package has only exact PyO3 0.30.1 with macros and abi3-py313. The rejected
Rust registry, serde conversions and their dependencies are removed. A new
source lock must be resolved and authenticated on CI, then consumed frozen and
offline by the original Wasm producer. Local dependency downloads, compilation
and guest execution are not part of this work.

The previous build script's library inspection smoke has NOT yet been adapted
to this primitive stage. Do not run that stale smoke or call its expected
library results proof of this port. The source-lock workflow runs first.

## Remaining qualification

First run real build/load/call for this new source and its object/type primitive
scope. Then implement original traversal and every reachable handler, preserving
actual schema/instance/registry objects and original Python error constructors,
regex/numeric/resolver behavior inside the budgeted Wasm guest. All 64 rich-error
cases, full 45 cold schema calls, 305 entries / 453 reads, original cache lifetime,
ordinary Flow and real product behavior remain required. No cap or acceptance
condition is waived by this primitive stage.

[Full original scope](../../../docs/migration/1520/schema-port-scope.md).
[Cleanup notes](../../../docs/migration/1520/cleanup-notes.md).
[Original type source](https://github.com/python-jsonschema/jsonschema/blob/v4.26.0/jsonschema/_types.py).
