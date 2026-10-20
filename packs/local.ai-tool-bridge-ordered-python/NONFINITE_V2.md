# Additive non-finite-output failure discriminator

The source-only v2 caller adapter distinguishes a non-finite **provider output**
from malformed request transport and original normalization errors. Its unchanged
original normalizer can parse `NaN`, `Infinity`, `-Infinity`, or overflowing float
tokens inside opaque argument JSON. The adapter raises
`NonFiniteProviderOutputError` instead of encoding those values as successful
DATA. The fixed message contains no input, output, exception text, or credentials.
Its `provider_failed` code is source-adapter metadata, **not a Core error code**,
and `retryable` is false.

The actual iterative v2 encoder raises a narrow
`NonFiniteJSONError(OrderedJSONError)` at non-finite float leaves. Only the
adapter's post-normalizer output encoding catches that discriminator. There is
no second tree traversal, independent budget, error-text matching, or copied
tree. The encoder's existing byte/node bounds and cycle checks apply, including
keys. Budget failures encountered before a non-finite leaf remain transport
exclusions; this is not a promise to classify unbounded output. The original
normalizer runs once. Its `GlobalContractInvocationError` remains distinct and
unchanged; invalid outer transport is rejected before it is called. The separate
v2 depth extension does not change the original inner parser or CoreStrictV1.

## Inspected old boundary

The old Broker validates output and computes its audit digest with
`allow_nan=False` (`tobkiri_runtime/tobkiri_host/broker.py:1273–1276,1797–1805`).
Non-finite output raises `ValueError`; dispatch records `provider_failed` and
raises the fixed `ProviderExecutionError` (`:1362–1388,1402–1422`).
`GlobalContractClient` and the production nested session do not translate that
exception into `GlobalContractInvocationError`. Therefore the actual Gateway
unwinds before its attempt/failover handler, which catches only the latter and
`GlobalContractUnavailable` (`gateway.py:559–596`). Even a synthetic contract
error with code `provider_failed` is outside the failover policy's allowlist
(`rumi_ai_pipeline_pack/runtime/pipeline.py:15,116`).

These are source-path findings. Dedicated tests execute the original normalizer
and exact extracted `_digest` function, not a complete Host invocation. Historical
HTTP exception-chain classification is not a requirement to recreate the retired
localhost HTTP surface.

## Pending guest and caller integration

The v1 codec, original source extraction, oracle, app, WIT, component, receipt and
staging remain unchanged. The separate `app_v2.py` source entrypoint now wires
the v2 adapter into the versioned WIT route. Source-level mapping tests are not
actual guest, native Core, production caller, or full Host replay.
The three historical NaN cases remain outside the v1 parity claim; the new result
is source-level terminal-provider-failure handling, not successful-output parity.

The versioned source entrypoint catches `NonFiniteProviderOutputError` before
its generic invalid/transport mapping and raises the existing canonical Core
`INTERNAL` error with a fixed non-secret message. This mapping still requires
actual generated bindings, component execution and native dispatch proof. Native Core currently projects
that to `internal` / `provider reported an internal error`, deliberately distinct
from the legacy exception class and code. Do not add `provider_failed` to Core's
closed enum, weaken CoreStrictV1, emit NaN DATA, or turn the failure into an
`invalid_response` retry trigger.

The migrated Gateway/caller must keep this outcome terminal and must not consult
provider failover or start another billable request. A `retryable=False` source
attribute alone does not enforce that policy after migration. Original
normalization-invalid handling remains separately classified, not swallowed by
this provider-output error.

Required proof remains: a separately versioned and source-bound guest build;
actual typed `INTERNAL` observations through real Core dispatch; unchanged finite
outputs and original-invalid handling; the actual migrated Gateway and four
inventoried caller edges with no retry on this failure; successful and denied
consumer permissions; exact dispatch/failed-outcome audit pairing; and public
session cleanup. Keep component replay, native acceptance, caller integration
and whole-unit migration claims separate.
