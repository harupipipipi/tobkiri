# Provider model access source oracle

This directory contains input-only cases and a generator that calls the canonical Python provider compiler directly. It does not contain hand-written expected values. The generator records what the original functions return or raise, the Python runtime that produced the observations, and whether they changed their arguments.

The source boundary is the complete canonical pair:

- `tobkiri_protocol/provider_compiler/model_access.py`
- `tobkiri_protocol/provider_compiler/native_filters.py`

The generator imports the source package from an explicit runtime root and refuses to run if the eight function bindings do not resolve to that root. It hashes all imported Python files under the supplied source root, the direct standard-library modules used by the source, the `decimal` module and `_decimal` implementation, and the invoked Python executable when available. The observation file also records the generator's resolved path, name, byte count, and SHA-256; full interpreter/build identity; Unicode version; `sys.get_int_max_str_digits()` and `sys.getrecursionlimit()`; Decimal library versions; and Decimal context before and after the case sequence. The source project's supported Python range remains unchanged; this fixture does not invent a package-level pin. For the current differential run, invoke it with the same CPython 3.13.9 interpreter used for the other fresh source observations and verify the recorded runtime receipt.

## Generate observations

Run from this directory with the selected CPython interpreter and the canonical runtime source path:

```sh
python3 generate_provider_model_access.py \
  --runtime-root /absolute/path/to/repo/tobkiri_runtime \
  --output provider-model-access.observations.v1.json
```

The default case file is `provider-model-access.cases.v1.json`. The source root is required; the generator never falls back to an installed package or copies the implementation into this fixture. It has no third-party Python dependencies. The generated observation file is a runtime receipt and is intentionally not checked in by this source-only preparation step.

## Case coverage

The finite cases cover legacy policy absence versus an explicit empty list, `all` and explicit policies, normalization order and de-duplication, model eligibility, reserved route overrides, and error precedence. They exercise official OpenRouter protocol/catalog/origin checks, the fixed native-filter revision, validation and ordering of discovery/routing fields, exact decimal-string normalization and bounds, discovery URL separation, and the original wire-boundary float conversion or exact-representation rejection. Two routing regressions pass list-of-pairs inputs where Python accepts an integer-key row before raising its later source `KeyError` or `TypeError`; they detect candidates that reject the first row at the ABI boundary. The source can also successfully return an integer-key mapping for related inputs, but this observation schema requires string-keyed JSON objects, so that successful shape is not represented in the shared parity cases and must be tracked separately.

One supplementary nonfinite case sends Python `float('nan')` to the original normalizer to preserve its exact rejection. It is marked `source-only-nonfinite` because the shared finite JSON ABI cannot represent that argument. It is not part of native/Wasm differential parity. All other cases use ordinary finite JSON values so both implementations can consume the same inputs.

Each case carries the source function outcome as the thin ABI envelope (`{"status":"ok","value":...}` or `{"status":"source-failure","exception_class":...,"message":...}`) and its compact UTF-8 JSON encoding in `outcome_json`. That string uses insertion order and no whitespace so it can be compared byte-for-byte with the candidate's returned string. Supplementary fields record the qualified exception class, before/after arguments, a structural non-mutation result, recursive Python type and mapping-key-order trees, and Python's default `json.dumps` text for successful function results. Float records include `repr` and `float.hex()` as extra evidence. These fields distinguish ABI bytes, typed ordered JSON equality, and Python's default serialization.

The compiler cases do not exercise credential access, registry ownership, revision/CAS authority, HTTP, or provider transport. Those stay outside this pure-data source boundary.
