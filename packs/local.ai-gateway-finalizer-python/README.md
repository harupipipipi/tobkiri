# Original Gateway finalization, source candidate

This bounded Python-on-Wasm source candidate extracts the reviewed production
`_finalize_generate_result` and `_finalize_stream_result` seams. It does **not**
implement full Gateway generation, routing, selection, credentials, retry,
failover, billing, production registration, or activation. The original Gateway,
its four caller identities and its legacy Host/PackVM path remain present.

## Typed boundary and error custody

The Pack-owned interface is
`local:ai-gateway-finalizer-python/finalizer@1.0.0`. `generate` and `stream` accept
an opaque ordered-finite-json-v2 string and return:

- `ok(finalized(payload))`: the ordinary original response, encoded once.
- `ok(domain-rejected({code, message}))`: an original Gateway domain rejection,
  such as a provider `status`/`error_code` rejected by `_normalize_result`.
- `err(Core error)`: a terminal import, transport, boundary, or projection failure.

The domain arm preserves the exact original code/message and remains distinct
from an imported Core `INVALID`, `UNAVAILABLE`, or any other Core error. A future
full Gateway caller can distinguish domain evidence without guessing from Core
codes. This helper never retries or decides whether a second billable attempt is
allowed. Every imported Core error record is rethrown unchanged. Malformed
successful dependency DATA and Python projection failures are terminal INTERNAL,
not domain rejections. Invalid boundary DATA is INVALID. Actual local
`RecursionError`/`MemoryError` is LIMIT_EXCEEDED. Imported records always retain
their own code, including when their message describes a resource failure.

The required imports are real generated bindings, not injectable production
providers or local fallbacks:

- `local:ai-gateway-tool-caller-python/caller@1.0.0`
- `local:ai-usage-cost-python/usage-cost@1.0.0`
- `local:ai-stream-normalizer-python/stream-normalizer@1.0.0`

Generate order is original result normalization, tool caller, usage calculator,
attempts attachment, then ordinary response projection. Stream order is stream
normalizer, **all** usage events, **all** tool events, top-level tools, then
ordinary response projection. The per-event loop calls `stream-intents`, not the
batch `stream-event-intents` import, so original partial-mutation order remains
visible within the local source execution. No successful partial response is
returned after an error, no rollback is introduced, and nothing is replayed.

## Recorded DATA, not authority

The exact envelope keys are `value`, `request_id`, `candidate`, `attempts` and
`packvm_response`. Stream also requires `attempt_number`, an exact positive
integer. `request_id` is a string; `attempts` is a list of objects. Candidate
contains the complete original dataclass field set:

- Strings: `model_id`, `provider_instance_id`, `catalog_provider_instance_id`,
  `catalog_revision`, `health`.
- String lists converted to original frozensets: `capabilities`, `modalities`.
- Exact integers: `context_length`, `priority`.
- Finite numbers or null: `input_cost`, `output_cost`, `health_observed_at`.
- Boolean `available`; object `raw`.

This is recorded candidate/provider DATA for the development slice. Supplying
IDs, prices, `available`, `approved`, or arbitrary metadata does not authenticate
a selected Candidate, own a budget, grant execution authority, or authorize a
provider call. The source preserves identity, pricing, unknown-cost and task-state
projection, including metadata precedence. Arbitrary provider metadata is not
exported as prompt or authority. Original tool normalization remains in the real
tool provider behind the existing tool caller, including its inert authority
flags and non-list intents fallback. The finalizer does not duplicate it.

## Ordinary response versus legacy PackVM

`packvm_response` is required and must be the exact boolean `false`. The old
numeric-string conversion is a legacy Host/PackVM transport requirement, not the
new Wasm DATA representation. This candidate uses finite native integers and
binary64 values, preserving negative zero and ordered object entries. It does
not import canonical Host serialization or silently fall back when PackVM is
requested. Production `_maybe_packvm_safe_response` remains unchanged.

Only two extracted functions have explicit transport adaptations:

1. `_tool_intents` delegates to the real imported caller, whose original helper
   performs normalization, successful Mapping filtering and dict projection.
   The finalizer validates the returned typed DATA list, without normalizing it.
2. `_maybe_packvm_safe_response` retains the ordinary `dict(value)` branch and
   rejects any value other than exact `False` for its `required` argument.

All other selected bodies, including Candidate, both finalizers, usage
projection, event loops, `_normalize_result` and `message_task_state`, are
complete verbatim source slices. `provenance.json` records source/output byte and
line extents, exact before/after text and hashes for both rewrites, and rationale.
The extractor rejects changed rewrite sources until reviewed explicitly.

## Bounds, reproducibility and evidence limits

The unchanged ordered-finite-json-v2 codec is staged by reference, never copied
into this Pack: 4 MiB UTF-8, 100,000 counted value/key nodes, 4,300 integer digits,
finite binary64 values and native ordered objects. Neither the recursion limit
nor the integer-conversion setting is changed. In-budget input does not promise
that original projection, dependency responses, or final output will fit those
bounds. Overflow and nonfinite computed costs are failures, never fabricated
zero/unknown successes. Native interpreter exhaustion is not guest capacity
proof. Wasm traps and fuel/memory exhaustion require their real Core handling.

Source-only preparation and verification:

    PYTHONDONTWRITEBYTECODE=1 python scripts/wasm/prepare_gateway_finalizer.py \
      --output packs/local.ai-gateway-finalizer-python/python
    PYTHONDONTWRITEBYTECODE=1 python scripts/wasm/prepare_gateway_finalizer.py \
      --output packs/local.ai-gateway-finalizer-python/python --verify

The extractor performs bounded no-symlink reads, preflights all output files,
rejects unsafe targets and binds full source/dependency hashes. Its inventory
binds the nine original finalization dependency edges in both profiles and the
two production seam call sites. Dependency evidence includes the current
reviewed caller/usage/stream original modules, adapters and apps, plus the
transitive tool-provider source and WIT, not merely interface names.
Whole-file source changes require deliberate
provenance refresh. Preparation does not build, download, execute a guest or
activate a provider.

`tests/test_gateway_finalizer.py` uses explicitly labeled generated-binding
doubles with the actual provider/caller Python sources. Independent original
Gateway AST oracles establish successful byte/order projection parity; the suite
also covers all nine Core error arms at every dependency-call position, domain
separation, malformed success, boundaries, actual native codec depth and node
expansion, source task-state semantics, and original partial mutation. Injected
MemoryError tests establish mapping only, not real exhaustion. Source
partial-mutation observations are not actual guest instrumentation.

`source-cases.json` is a small deterministic DATA catalog for subsequent composed
replay, not a receipt or expected-output implementation. It includes ordinary
generate/stream success, original domain rejection, later stream usage/tool
failure, nonfinite cost from finite/string inputs and absent usage. The source
suite compares successful cases against the original helpers. A real five-Pack
composition (finalizer, tool caller, tool provider, usage and stream normalizer),
producer/runtime receipts, actual imported dispatch/error parity, native Core
Gate/owner/consumer evidence and production activation remain separate gates.
