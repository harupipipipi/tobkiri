# Additive bounded-integer source profile

`ordered_json_v2.py` and `python/caller_adapter_v2.py` define
`ordered-finite-json-v2` without modifying any v1 staged source, frozen oracle,
component, receipt, WIT, native acceptance fixture, or active provider.

The integer value-domain expansion admits exact native integers up to 4300 decimal
digits, excluding the sign. They remain ordinary JSON decimal numbers inside
the opaque WIT string, never floats, tags, or quoted numeric replacements.
Encoding checks signed magnitude before decimal conversion; decoding checks
token length before conversion. The adapter requires the interpreter's existing
4300-digit limit and never changes it. Original inner-JSON parsing is unchanged:
4301-digit inner integers still raise the original `ValueError`, not an invented
normalization error or successful result.

The existing 4 MiB UTF-8 and 100,000-node (including keys) budgets,
finite binary64, exact-native-type, Unicode, duplicate-key and cycle rules remain.
The unshipped v2 profile now traverses iteratively without v1's separate depth-64
cap; node and byte budgets bound traversal. The standard decoder's actual
recursion failure remains a runtime resource boundary, not a universal accepted
depth promise. See `DEPTH_V2.md`.
The reused unchanged v1 scalar helpers are explicitly source-bound.
The per-integer bound plus document/node bounds limits aggregate conversion work;
it does not prove execution within Wasm fuel, memory or wall-clock budgets.
Native CoreStrictV1's separate 4 MiB *outer JSON* limit is unchanged. Quoting can
exceed it even when the inner document fits. Only measured observation envelopes
are claimed to fit; no 4 MiB inner-capacity promise is made.

## Exact local source proof

Run the standard installed reference interpreter, not an unqualified `python`:

```
PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3.13 \
  scripts/wasm/observe_ordered_integer_v2.py --output <fresh-file.json>
```

The script requires actual CPython 3.13.5, Unicode 15.1.0 and the active
4300-digit limit. It validates the untouched historical oracle, re-observes all
33 original outcomes, and compares the v2 adapter's real outputs against
independent standard JSON serialization of the actual original results. It
records exact output bytes, source bindings, and original digit-boundary errors.
Sources are checked before and after observation.

`integer-v2.observations.json` reports 18 successful source comparisons,
12 original normalization-error comparisons, and three remaining error-boundary
differences.
The three bigint and three depth cases move from v1 exclusions to successful
**source-adapter** comparisons. The preserved v1 component evidence remains 24 admitted cases plus
nine exclusions. NaN is a fixed, nonretryable source-provider failure rather
than invalid caller input; it remains an intentional typed-boundary difference,
not exact legacy Host/HTTP error parity. See `NONFINITE_V2.md`. This is not full
Host replay or whole-unit parity.

## Pending component and production proof

The separate v2 guest source and staging helper bind a versioned ABI; their
source tests are not component execution. The existing v1 build plan and
receipt remain byte-identical and must continue to validate. Do not modify
their source hashes or relabel their observations as v2 evidence.

A replacement requires an explicit versioned v2 Pack/WIT route and caller,
an independently reviewed source plan binding both codecs and the new adapter,
the immutable v1 oracle plus the supplemental exact-output evidence, and a real
producer receipt. Expected outputs and observation code stay outside the guest.
Then replay the actual new guest with the reviewed CPython/Unicode/digit-limit
profile and existing fuel/memory/time bounds, retaining all exclusions.
Finally perform the native Core owner/consumer, permission-denial and exact
audit acceptance with the new artifact and pins. Keep the old provider/gateway
until those gates and the remaining behavior gaps are closed. This source-only
proof does not close the migration inventory or authorize provider retirement.

## Separate component evidence

The dated `tools/python313-producer/trials/2026-10-20/` records a later real v2
component build and Wasmtime 48 development replay. All 33 reviewed cases ran:
18 exact success strings, 12 typed INVALID mappings and three distinct INTERNAL
nonfinite checks; each case completed guest init and shutdown. The running guest
reported Python 3.13.16 / Unicode 15.1.0. This does not retroactively turn the
source-observation flags into execution evidence. Native Core dispatch, actual
Gateway integration and production activation remain separate pending gates.
