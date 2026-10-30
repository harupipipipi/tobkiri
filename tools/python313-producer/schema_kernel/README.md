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
source lock was resolved by CI 41065264251 at source e04117faa and authenticated
against its actual source/manifest/lock receipt. It is consumed frozen and offline
by the original Wasm producer. This proves dependency provenance only. Local
dependency downloads, compilation and guest execution are not part of this work.

The build script now qualifies only `original-object-primitives/1`: actual root
and nested reference identity, mutation aliases, 21 type cases and the private
unknown-type ValueError. `numbers` is imported during module initialization;
object creation and primitive observations run only in the actual DATA request.
The receipt explicitly records zero implemented handlers and no schema validation.
Actual CI 41065687808 / job 124096340005 succeeded at source e436a6d5f:
extension build, limited-API/dynamic-link inspection, original producer import
and component generation, then the real Core NativeData call with these checks.
This proves only the primitive scope under the unchanged 64 MiB / 2-billion fuel /
original 30-second deadline. A primitive success must never be reported as full
validator, OS-owner/GUI proof or 64-case parity.

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
