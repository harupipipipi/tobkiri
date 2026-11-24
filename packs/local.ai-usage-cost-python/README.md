# Original usage-cost provider: source-only Python-on-Wasm candidate

This additive candidate extracts `create_cost_operation` and `_number` from the
existing `rumi_ai_usage_pack/runtime/usage.py`, retaining complete source lines
byte for byte. It includes only their original `__future__`, `math`, and `typing`
imports. The tokenizer, Host factory, registrations, credentials and broker
plumbing are not included. This is a necessary dependency of the bounded Gateway
response-finalization slice, not a substitute Rust implementation or a claim of
complete Gateway/Pack migration.

## Typed surface and aliases

The WIT package is `local:ai-usage-cost-python@1.0.0`. Its `usage-cost` interface
exports `calculate(operation: string, payload: string) -> result<string, error>`
and diagnostic `runtime-info() -> string`. It includes canonical
`tobkiri:core/pack-minimal@1.0.0`, with the unchanged Core error type and
resource-free lifecycle. There is no fake Host, local authority substitute,
credential path, or provider-import fallback.

Accepted operation identities are explicit:

- `calculate` and `normalize`: original pure-provider aliases, unchanged.
- `rumi_ai_usage_pack.ai-usage-cost.generate` and `.stream`: original aliases,
  unchanged.
- `rumi_ai_usage_pack.ai-usage-cost`: original manifest identity mapped to
  `calculate`, as the original `AIUsageHostFactoryV4` does. The pure function
  itself does **not** accept this base identity directly.

The operation returns the original input/output/total token projection, cost,
currency, known flag, usage provenance and pricing revision. Missing, invalid,
negative and nonfinite numeric values retain the original unknown/null behavior.
Booleans and numeric strings retain the original `float()` conversion behavior.
Primary token keys retain their original precedence over fallback keys, including
an explicitly present null primary value. The source is a cost **projection**,
not billing, reservation, credit, payment or budget authority.

## Finite DATA boundary and terminal failures

Payload/result strings use the unchanged `ordered-finite-json-v2` codec, not
Core JSON object semantics. Exact native dict/list/string/null/bool/int/finite
float DATA retains insertion order, integer identity, binary64 floats and
negative zero. The codec retains its 4 MiB UTF-8, 100,000-node and 4,300-digit
integer bounds. No recursion limit or integer-conversion setting is changed.
An object is required at the outer input boundary; additional object members
remain DATA and are handled/ignored by the unchanged original function.

This is DATA-domain parity, **not** parity for arbitrary direct Python objects.
Custom mappings, numeric objects implementing `__float__`, generators, tuples,
subclasses, non-string dict keys, and nonfinite numeric DATA do not cross this
transport. The original direct Python function may accept some such objects.
Strings such as `"Infinity"` are ordinary finite DATA and still follow the
original conversion-to-unknown behavior.

Typed failures are fixed, terminal and never replay the provider:

- Malformed/over-budget input, unknown identity, non-object input or changed
  integer-conversion safety setting: `INVALID`, `invalid usage cost request`.
- Original `TypeError`/`ValueError`: `INTERNAL`,
  `usage cost provider validation failed`.
- Original `OverflowError` or a nonfinite/overflow output: `INTERNAL`,
  `usage cost provider arithmetic failed`.
- Other output encoding/domain/budget failure: `INTERNAL`,
  `usage cost provider output failed`.
- Actual `RecursionError`/`MemoryError`: `LIMIT_EXCEEDED`,
  `usage cost resource limit exceeded`.

Finite operands can overflow sums/products, and a valid 4,300-digit input integer
can overflow conversion to float. Neither failure becomes zero, null, unknown,
or another successful result. Private exception text is not exported. These
categories do not claim legacy HTTP error-text parity. This pure provider cannot
emit every Core error arm; no synthetic authorization/cancellation cases are
invented as runtime evidence.

## Reproduce and stage the source evidence

Run from the repository root:

```sh
PYTHONDONTWRITEBYTECODE=1 python scripts/wasm/prepare_gateway_finalization_providers.py \
  --provider usage-cost --output packs/local.ai-usage-cost-python/python
PYTHONDONTWRITEBYTECODE=1 python scripts/wasm/prepare_gateway_finalization_providers.py \
  --provider usage-cost --output packs/local.ai-usage-cost-python/python --verify
```

`provenance.json` records original file hashes, exact line/byte ranges, output
ranges and slice hashes. `inventory.json` binds all three original manifests,
both profiles, generate/stream/text/messages caller edges, original Host mapping,
and the text/messages wrappers' actual delegation calls. The wrappers and whole
Gateway are inventoried, not migrated by this provider. Their whole-source hashes
must be deliberately regenerated and reviewed when the Gateway helper seam or
original generated metadata changes; no unrelated evidence is silently repaired.

The provenance `staged_references` declares unchanged codec files from
`packs/local.ai-tool-bridge-ordered-python/ordered_json.py` and
`ordered_json_v2.py` for `python/` in a future staging root, and canonical
`crates/tobkiri-wit/wit/core/core.wit` for `wit/deps/tobkiri-core/core.wit`.
Each is hash-bound and must be copied unchanged after verification. No serializer
copy or rewrite is checked into this candidate, and preparation does not perform
staging, production, guest execution or activation. Stage this pack's own
`python/` modules and WIT separately from the stream provider.

The focused tests independently compile original source AST nodes as the
behavioral oracle and use clearly labelled generated-binding value/class doubles
only to test the source wrapper's typed records. They include exact extraction,
closure, aliases, arithmetic limits, codec boundaries and runtime failure mapping.
Real decoder exhaustion and injected resource exceptions are labelled separately.
These checks are not real Wasm/Core evidence. Producer execution, independently
observed guest interpreter/Unicode versions, component composition, native
routing/Gate/error/audit custody, materialization, and production activation
remain separate acceptance gates. No download, guest, remote write or activation
is performed by this source-only work.
