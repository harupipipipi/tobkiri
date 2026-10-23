# Pure provider model-access Rust candidate

This additive, unpromoted helper translates the canonical pure modules
`tobkiri_runtime/tobkiri_protocol/provider_compiler/model_access.py` and
`native_filters.py`. It owns no Registry, credentials, UI, approval state,
network transport, freshness proof or route authority. The original full
API/key/baseURL/model GUI and its owners remain the integration source.

Cargo package: `tobkiri-pack-provider-model-access-rust`. New helper WIT:
`local:provider-model-access-rust/compiler@1.0.0`; it does not impersonate an
existing migrated v4 provider. Lifecycle is resource-free. Its only world
include is the existing Core `pack-minimal`; it does not call its imports.

## Thin observation ABI

`invoke(operation, payload)` takes a snake_case original public function name
and an ordered-finite-json-v2 object of Python keyword arguments:

| Operation | Required kwargs |
| --- | --- |
| `normalize_model_access` | `value`, keyword-only `connection` |
| `effective_model_access` | `connection` |
| `model_is_allowed` | `connection`, `model_id` |
| `compile_connection_parameters` | `connection`, `model_id`, `parameters` |
| `is_official_openrouter` | `connection` |
| `normalize_native_filters` | `value`, keyword-only `verified_openrouter` |
| `discovery_url` | `native_filters` |
| `apply_native_routing` | `native_filters`, `parameters` |

The returned DATA string is exactly one of:

```json
{"status":"ok","value":null}
{"status":"source-failure","exception_class":"ValueError","message":"..."}
```

The second is an original-source exception observation, never a successful
production operation. Missing/extra function kwargs are source TypeErrors;
missing nested fields differ from explicit null. Unknown operation, non-object
kwargs, unsupported finite representation and codec failures remain outer
Core errors. The shared codec's byte/node/integer limits remain unchanged.
No source error is mapped to a fabricated permission or successful route.

Policy IDs sort/deduplicate; native token lists deduplicate in first-seen
order. Object order and ordinary parameter types remain visible. Normalized
prices retain integers or exact decimal strings with redundant fractional
zeroes removed. Only the original wire `apply_native_routing` boundary converts
normalized prices to int/binary64, checking Python's decimal round-trip with
the shared Python float spelling. Policy price normalization never uses float.

The local URL helper implements only `urlsplit` observations read by the
official OpenRouter predicate, including eager bracket/NFKC checks and lazy
port validation. It is not a general URL validator and supplies no authority.
The bounded Python integer-conversion assumption is 4300 decimal digits, as
in the shared codec/source runtime; the fresh original-source receipt attests
that setting and the native consumer checks it. Keyword diagnostics
follow CPython 3.13.9 [argument binding](https://github.com/python/cpython/blob/v3.13.9/Python/ceval.c#L1446-L1467)
and [suggestion costs](https://github.com/python/cpython/blob/v3.13.9/Python/suggestions.c#L6-L164),
including raw keyword spelling and declaration-order ties.
Its Unicode delimiter and IPv6 compatibility still require independent edge
coverage before promotion. Direct `dict(parameters)` inputs that create
non-string Python keys have no representation in `PyValue`; they return an
outer boundary error rather than an invented original-source exception.
The current shared finite corpus does not exercise that richer Python domain.

## Independent original-source comparison

The ignored native differential test requires all three variables:

- `TOBKIRI_PROVIDER_MODEL_ACCESS_ORACLE_RECEIPT`: absolute original
  `observations.json` path.
- `TOBKIRI_PROVIDER_MODEL_ACCESS_ORACLE_SHA256`: explicit lowercase 64-hex
  receipt hash, without `sha256:`.
- `TOBKIRI_PROVIDER_MODEL_ACCESS_ACTUAL_DIR`: NEW absolute private directory.

Test name: `all_finite_original_observations_match_or_retain_exact_mismatches`
in `tests/oracle_receipt.rs`. Missing inputs fail when this ignored test is
explicitly selected. The consumer checks the pinned 80-row original receipt,
the unchanged case corpus hash, runtime identity and original source bytes.
It preserves ordered raw arguments, compares all 79 finite ABI outcomes,
exception class/message, recursive Python type/float-bit trees and key order,
and retains every actual before reporting mismatch. The one direct-source
NaN probe stays visible as supplementary evidence and is not replayed as a
fabricated finite input. Fresh directories and files cannot overwrite evidence.

Native comparison is only one stage. The same finite fixture payloads must
also cross the actual Wasm/Core route before guest parity is claimed. No
build/test or actual parity is claimed by this source candidate.

## Relationship to saved settings

Provider Registry remains the connection/policy owner; Model Registry retains
model IDs, parameters, aliases and exact connection references. Gateway still
resolves the current enabled connection, adapter re-reads it and applies this
compiler before credential/HTTP work. A supplied snapshot or revision never
proves its own authority or freshness. Any later Core integration must preserve
these checks, Registry CAS, credential create/revoke and scoped binding, and
failure recovery. Existing `secrets.get/reveal` and `network.fetch` do not by
themselves establish that settings-update equivalence. This helper supplies
neither fixed model/baseURL replacements nor full settings migration.
