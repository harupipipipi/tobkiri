# Original stream normalizer: source-only Python-on-Wasm candidate

This additive candidate extracts the original `_ALLOWED_TYPES` and
`create_stream_normalize_operation` from
`rumi_ai_stream_pack/runtime/normalizer.py`. It retains complete source lines
byte for byte, the original `__future__`, `collections.abc.Iterable`, and `typing`
imports, and the original `GlobalContractInvocationError` class from Core source.
Host factories, registrations, credential and broker plumbing are omitted.
It is a required dependency of a bounded Gateway response-finalization slice,
not a Rust replacement or complete Gateway/Pack migration.

## Typed surface and original semantics

The WIT package is `local:ai-stream-normalizer-python@1.0.0`. Its
`stream-normalizer` interface exports
`normalize(operation: string, payload: string) -> result<string, error>` and
`runtime-info() -> string`. The world includes canonical
`tobkiri:core/pack-minimal@1.0.0` and its error type/resource-free lifecycle.
Accepted identities are the original `normalize`, `validate`, and
`rumi_ai_stream_pack.ai-stream-normalize`, passed to the source unchanged.
There is no fake Host, local authority substitute, or provider-import fallback.

The original source accepts `value` as an event collection or an object with
`events`, and preserves event order. Allowed types remain `text_delta`,
`thinking_delta`, `tool_intent_delta`, `usage`, `finish`, and `error`. Every
normalized event has exactly the original projected key order:
`request_id`, `sequence`, `type`, `delta`, `tool_intent`, `usage`, `finish_reason`,
`error_code`, `provider_attempt`. Input event binding/sequence fields do not
replace the top-level request/attempt bindings. Extra event fields are discarded.
Request IDs retain the original `str(...).strip()` behavior and attempts retain
`int(payload.get("provider_attempt") or 1)`, including its default and coercions.

An empty stream, missing terminal, unsupported type, malformed event, or any event
after either `finish` or `error` fails. No synthetic terminal is added. `finish`
and `error` are successful DATA event types when properly terminal; their names
do not manufacture a typed Core error. No local retry, replay, truncation, repair
or rollback is introduced.

## Finite DATA boundary and typed failures

Payload/result strings use the unchanged `ordered-finite-json-v2` codec, not
Core object semantics. Exact native finite DATA preserves insertion order,
integers, binary64 floats, negative zero, and opaque strings. Existing 4 MiB
UTF-8, 100,000-node and 4,300-digit integer bounds apply. Neither interpreter
safety setting nor recursion limit is changed. The boundary requires an outer
object but retains original event/request/attempt validation inside the provider.

The parity domain is DATA, **not** arbitrary Python objects. The direct source
can accept tuple/generator events, custom mappings and custom `__str__`/`__int__`
objects, while these are deliberately rejected by the finite native transport.
Nonfinite numeric DATA and dict keys other than exact strings cannot cross it.
Nested finite event fields are forwarded unchanged; they are not authority.

All failures are terminal with fixed messages, not private exception text:

- Invalid identity, malformed/over-budget codec input, non-object input or changed
  integer safety setting: `INVALID`, `invalid stream normalizer request`.
- Original `GlobalContractInvocationError`, `TypeError` or `ValueError`:
  `INTERNAL`, `stream normalizer provider validation failed`.
- Original overflow or nonfinite/overflow output: `INTERNAL`,
  `stream normalizer provider arithmetic failed`.
- Other output encoding/domain/budget failure: `INTERNAL`,
  `stream normalizer provider output failed`.
- Actual `RecursionError`/`MemoryError`: `LIMIT_EXCEEDED`,
  `stream normalizer resource limit exceeded`.

Projection can expand a valid input beyond the output byte/node budget because
the request ID and all projected fields are repeated per event. This becomes a
terminal output failure, never a truncated successful stream. These categories
make no legacy HTTP error-text parity claim; the other Core error arms are not
fabricated for a pure provider that cannot emit them.

## Source evidence and future staging

From the repository root:

```sh
PYTHONDONTWRITEBYTECODE=1 python scripts/wasm/prepare_gateway_finalization_providers.py \
  --provider stream-normalizer --output packs/local.ai-stream-normalizer-python/python
PYTHONDONTWRITEBYTECODE=1 python scripts/wasm/prepare_gateway_finalization_providers.py \
  --provider stream-normalizer --output packs/local.ai-stream-normalizer-python/python --verify
```

Provenance binds original hashes, exact line/byte/output offsets and slice hashes.
Inventory binds the three original manifests, both profiles and the original
stream caller edge. Generate/text/messages do not gain a stream-normalizer edge:
their inspected delegation sources are explicitly recorded as not migrated and
not using this provider. Whole-Gateway and original metadata hashes must be
explicitly regenerated and reviewed when that separate helper integration changes
them, rather than silently altering unrelated pins.

The `staged_references` in provenance declares the existing, unchanged
`packs/local.ai-tool-bridge-ordered-python/ordered_json.py` and
`ordered_json_v2.py` at `python/` in a future staging root, and canonical
`crates/tobkiri-wit/wit/core/core.wit` at `wit/deps/tobkiri-core/core.wit`.
Those files are hash-bound dependencies, not copied serializers in this candidate.
Copy them unchanged after verification and stage this pack's `python/` source and
WIT as a separately owned provider. This extractor performs source preparation
only, not staging, component production, execution or activation.

Tests compare unchanged extracted functions against independently selected
original AST source, including all allowed types, exact projection order, terminal
validation, coercions, finite DATA and output expansion. Clearly labelled
source-binding doubles test the wrapper's exact typed failures only. Injected
resource mapping is distinguished from a real native decoder exhaustion case.
Neither proves a guest or native Core boundary. Producer execution, actual guest
interpreter/Unicode observations, component composition, native routing/Gate/
audit/error custody, materialization and activation remain separate acceptance
gates. This work performs no download, guest execution, remote write or production
activation.
