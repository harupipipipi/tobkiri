# Original Gateway normalization callers: source-only candidate

This additive candidate extracts the exact original `_tool_intents` and
`_attach_stream_tool_intents` functions, their four required constants, original
stdlib imports, and original `GlobalContractInvocationError` class. Complete
source lines remain byte-identical; `python/provenance.json` binds file hashes,
line ranges, byte offsets, output offsets, and slice hashes. Preparation never
executes the original Gateway, imports the legacy Host, builds a component,
modifies a runtime profile, or activates this Pack.

## Deliberately bounded scope

The original generate, stream, text-generate, and messages-generate caller edges
are inventoried against both profiles and all three original bridge manifests.
Wrapper source and actual delegation calls are pinned, but text/messages
wrappers, `_invoke`, model selection, provider generation, failover, budget and
billing logic, and Host registration **are not migrated**. Four original caller
edges are not four exported caller identities. These tests do not prove complete
Gateway behavior, paid-provider behavior, runtime Core dispatch, or audit events.

The new WIT world retains canonical Core `pack-minimal`, adds a **required**
`local:ai-tool-bridge-ordered-python/bridge@2.0.0` import, and exports only its own
`caller` interface:

- `generate-intents(payload: string)`: `{request_id: string, intents: DATA}` input;
  returns the original helper's list of normalized intents.
- `stream-intents(payload: string)`: the same shape, choosing the original stream
  bridge operation.
- `stream-event-intents(payload: string)`: `{request_id: string, events: list<object>}`
  input; returns the event list after the unchanged helper processes it.

`intents` is any value in the bounded finite DATA codec. Non-list values pass
unchanged to the real original bridge, which owns its existing empty-list
fallback. The caller transport does not duplicate that normalization or relax
the exact envelope, request ID, codec limits, or stream-event object checks.

These exact object shapes and string request IDs deliberately bound the new
caller input domain. The original helper's Python annotations do not validate
arbitrary direct Python inputs, while production `_invoke` supplies a string
request ID. This candidate does not claim parity for arbitrary direct helper
objects such as a dictionary-valued request ID. Invalid export input is rejected
before the bridge import is called.

All strings use pack-owned `ordered-finite-json-v2`, not CoreStrictV1 objects.
Exact insertion order, integers, binary64 floats, negative zero, and opaque inner
argument JSON remain DATA. The codec's unchanged 4 MiB byte, 100,000-node and
4,300-decimal-digit integer bounds apply. There is no new depth limit. Actual
interpreter/guest resource exhaustion can still occur. The outer canonical Core
request/response envelopes have independent limits and must be checked during
later native integration.

The adapter calls the generated typed `wit_world.imports.bridge.normalize`
exactly once per `_tool_intents` invocation, with only the original contract and
`.generate`/`.stream` operation IDs permitted. It has no injected runtime transport,
local normalizer, HTTP, Host, alternate authority, or fallback. Stream event
normalization remains one matching event at a time, in order, with each original
single-intent call starting at fallback index zero. Earlier mutations remain if
a later event fails; the helper does not roll back or replay them.
One `stream-event-intents` export call can therefore make multiple bridge imports.

The separate `runtime-info() -> string` export reports the executing
interpreter's actual Python and Unicode versions using the same ordered codec.
It is diagnostic, not a fourth migrated Gateway identity. Real replay must
observe both caller and provider interpreter versions instead of inferring the
caller runtime from a producer receipt.

## Error custody and intentional fail-closed boundary

Every imported typed Core error, including INVALID, DENIED, APPROVAL_REQUIRED,
CANCELLED, and all other arms, is retained unchanged in a terminal exception
that is **not** `GlobalContractInvocationError`. The app rethrows the same typed
error record. No error arm is mapped into a locally retryable Gateway failure.
The real legacy nested Host Broker wraps provider exceptions into terminal
`ProviderExecutionError`; pre-dispatch authorization failures remain distinct.
A direct fake-client/source call can have different retry behavior and is not
used as evidence for that real boundary.

Request-encoding/import-transport failures and malformed successful responses
are terminal, without normalization replay. Successful response shape checks in
the original helper remain verbatim, including filtering non-mapping intent
items and taking item zero for stream events. The enclosing adapter turns helper
shape failures into a fixed terminal INTERNAL result. This is an intentional
fail-closed boundary for malformed provider success, **not** a claim of legacy
malformed-response retry parity. Private exception text is not used as success
DATA or substituted for a typed Core failure.

## Reproduce source evidence

Run `scripts/wasm/prepare_gateway_tool_caller.py --output <directory>`, then use
`--verify` against the same directory. All reads are bounded regular-file reads;
source and output ancestor symlinks are rejected. Existing output hard links and
nonregular files are rejected before any output is changed. Verification is
read-only and binds omitted original Gateway/wrapper source as well as selected
slices. The existing two codec modules, canonical Core WIT, and original bridge
v2 WIT are hash-bound dependencies, not untracked copies in this candidate.

The adapter tests explicitly install generated-binding **transport fixtures** to
exercise source-level control flow. The ordinary bridge normalizer is exercised
only by a clearly named source fixture. Neither fixture supplies production
imports or establishes Wasm execution, native Gate enforcement, dispatch, audit,
or provider authority. Building, real component composition and replay, native
materialization and acceptance, and production activation are separate gates.
