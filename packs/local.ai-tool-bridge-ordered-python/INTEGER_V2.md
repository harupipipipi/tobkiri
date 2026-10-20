# Additive bounded-integer source profile

`ordered_json_v2.py` and `python/caller_adapter_v2.py` define
`ordered-finite-json-v2` without modifying any v1 staged source, frozen oracle,
component, receipt, WIT, native acceptance fixture, or active provider.

The only value-domain expansion is exact native integers up to 4300 decimal
digits, excluding the sign. They remain ordinary JSON decimal numbers inside
the opaque WIT string, never floats, tags, or quoted numeric replacements.
Encoding checks signed magnitude before decimal conversion; decoding checks
token length before conversion. The adapter requires the interpreter's existing
4300-digit limit and never changes it. Original inner-JSON parsing is unchanged:
4301-digit inner integers still raise the original `ValueError`, not an invented
normalization error or successful result.

The existing 4 MiB UTF-8, root-zero depth 64, 100,000-node (including keys),
finite binary64, exact-native-type, Unicode, duplicate-key and cycle rules remain.
The unchanged v1 scanner and scalar helpers are explicitly source-bound.
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

`integer-v2.observations.json` reports 15 successful source comparisons,
12 original normalization-error comparisons, and six remaining exclusions.
The three bigint cases move from v1 exclusions to successful **source-adapter**
comparisons. The preserved v1 component evidence remains 24 admitted cases plus
nine exclusions. The three depth feature gaps and three NaN error-boundary
differences remain; this is not full Host replay or whole-unit parity.

## Pending component and production proof

No v2 component is built or staged by this change. The existing v1 build plan
and receipt remain byte-identical and must continue to validate. Do not modify
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
