# Private schema kernel comparison

This is source for an unproved comparison extension, not a product replacement.
It has not been compiled, loaded, or called. It changes no production import,
schema loader, cache, validation, semantic checks, or Python dependency.

The standalone package is `tobkiri-schema-kernel` 0.1.0. Its only library is
the `_tobkiri_schema_kernel` `cdylib`. `jsonschema` is exactly 0.60.2 with default
features disabled and only `pyo3` enabled. The direct PyO3 dependency is exactly
0.30.1 with `abi3-py313` enabled. HTTP, file retrieval, TLS, asynchronous
retrieval, code generation, and IDNA features are not selected.

## Observation API

```python
report = _tobkiri_schema_kernel.inspect(
    schema_json, actual_python_instance,
    resources_json="{}", validate_formats=True,
)
assert report["original_instance_root"] is actual_python_instance
```

`schema_json` is JSON text for a schema. `resources_json` is a JSON object
mapping explicit resource URIs to schema values. The registry has an explicit
retriever that always refuses retrieval; validation options also use
`offline()`. Local `$ref` processing stays in the actual library. Each call
builds a fresh registry and validator; there is no global validation cache.

The runtime validator uses `options_for::<json::Pyo3>()` with Draft 2020-12 and
reads the Python instance in place. `json::inspect` surrounds validation and
descriptor rendering, holding visited Python objects and propagating errors
reported by that representation. Unsupported input errors are Python errors,
not successful validation results.

The report distinguishes `schema-build` failures (`compiled=False`,
`valid=None`) from `instance-validation` (`compiled=True`). It includes the
engine's message, keyword, instance clone, JSON Pointer locations, absolute
keyword URI, available branch contexts, property-name error and Rust source
message. It keeps the engine's observed error sequence and branch grouping.
Parsing or registry preparation errors raise `ValueError`.

For a later schema-selfcheck comparison, use the oracle's original
`meta_schema` as the compiled schema, map `meta_registry` entries by URI, and
pass each actual Python schema as `actual_python_instance`. This calls the
actual engine against those inputs; it does not claim to implement Python
`Draft202012Validator.check_schema` or reproduce its first-error selection.

## Known boundaries

- Schemas and registered resources must become `serde_json::Value`. Original
  Python schema references and insertion order are lost at that boundary.
  The default serde map representation is sorted, and compiler keyword/error
  order may differ from Python's. Nothing restores the oracle's order.
- `ValidationError::instance()` returns a serde value even with the Python
  instance backend. `instance_clone` is explicitly a clone. The separate
  `original_instance_root` preserves only the caller's actual root reference;
  it proves no engine-held reference to an individual failing node.
- There is no public original-Python error-instance accessor in the inspected
  API. There are no Python `SchemaError`/`ValidationError` objects, original
  meta-schema references, `validator_value` reference guarantees, parent links,
  or original cause exception objects here. `source_message` is only the Rust
  error source's text. JSON Pointer strings are not Python relative/absolute
  path deques. Context branch grouping is not Python exception context parity.
- Error descriptors are serialized to JSON and decoded by Python's `json`
  module. They lose original references and object order too. This conversion
  is comparison overhead and supplies no size or fuel improvement evidence.
- Formats, regular expressions, numeric edge cases, reference resolution,
  schema validity and error text/order still require actual comparison. IDNA
  formats are unavailable without the disabled `idna` feature. Standard serde
  numeric ranges are used; arbitrary-precision features are not selected.

Do not add a schema-path reconstruction, fallback engine, or broad error
correction layer to disguise these gaps. If the required rich error/object
contract is unavailable, reconsider a direct port of the original engine.

## Required CI sequence

1. Resolve and bind a source-only lock and verified vendored registry closure.
   No local build, dependency-source download, or local execution is part of
   this work. The root workflow owns these receipts and later CI stages.
2. First build this exact package for `wasm32-wasip2` using the saved producer's
   [rpds recipe](../build_rpds.py): its Python 3.13/abi3 cross configuration,
   WASI SDK linker, frozen/offline Cargo invocation, dynamic import link flags,
   and `pyo3_disable_reference_pool` flag. Substitute this crate's source/vendor
   mounts only. The crate's `abi3-py313` feature must stay enabled. A filename or
   manifest declaration alone is not proof of the resulting extension's ABI.
   Have that original producer build the component, import the actual emitted
   extension and call `inspect` inside the guest. Record build/load/call and
   actual limited-API evidence before declaring even prototype success.
3. Only then compare all 64 [original oracle](../schema_kernel_oracle.py) cases
   including rich errors, original object references, parent/cause links and
   ordering. Missing fields remain explicit failed/unavailable comparisons.
4. Only after that gate, attempt the full catalog's 45 cold calls, original
   cache lifetimes, 305 entries, 453 reads and refusal cases. Re-measure artifact
   size and the original budget; retain every required read and validation.

The Python engine/dependencies can be removed only after replacement parity,
the real product connection and other consumers have been established. See
[cleanup entry 004](../../../docs/migration/1520/cleanup-notes.md).

## Primary API references

- [jsonschema 0.60.2 feature and dependency declarations](https://docs.rs/crate/jsonschema/0.60.2/source/Cargo.toml.orig)
- [Runtime representation contract: schemas remain serde values](https://docs.rs/jsonschema/0.60.2/jsonschema/json/index.html)
- [Pyo3 representation and borrowed Python node type](https://docs.rs/jsonschema/0.60.2/jsonschema/json/struct.Pyo3.html)
- [Python representation inspection scope](https://docs.rs/jsonschema/0.60.2/jsonschema/json/fn.inspect.html)
- [Registry builder and preparation](https://docs.rs/jsonschema/0.60.2/jsonschema/struct.RegistryBuilder.html)
- [Validator options, registry and offline mode](https://docs.rs/jsonschema/0.60.2/jsonschema/struct.ValidationOptions.html)
- [ValidationError accessors](https://docs.rs/jsonschema/0.60.2/jsonschema/error/struct.ValidationError.html)
- [ValidationErrorKind branch context](https://docs.rs/jsonschema/0.60.2/jsonschema/error/enum.ValidationErrorKind.html)
- [PyO3 0.30.1 ABI features](https://pyo3.rs/v0.30.1/features.html#the-abi3-pyxy-features)

The official docs' `latest` pages identified themselves as 0.60.2 during
inspection; the links above pin that release for future readers.
