# Original Python tool bridge: ordered transport candidate

This is a separately versioned migration candidate, not an activated provider or
proof of final native Core acceptance. The Pack WIT string domain is
`ordered-finite-json-v1`; the copied canonical Core WIT is unchanged.

The original normalizer and `GlobalContractInvocationError` are captured verbatim
from the repository, with byte-slice provenance. Host factories, capture plumbing,
contract clients and authority imports are not present in the guest. All three
registered operation IDs follow the original Host factory's `normalize` mapping.
All four original caller edges are inventoried in `python/inventory.json`.

## Domain

Both request and successful response are bounded by 4 MiB UTF-8, depth 64 (root
zero), and 100,000 nodes including keys. Allowed exact native types are null,
bool, safe integer (absolute value at most 2^53-1), finite binary64, string, list,
and insertion-ordered string-keyed dict. Floating 1.0 stays distinct from integer
1 and negative zero keeps its sign. Outer duplicate keys, surrogates, cycles and
subclasses are rejected. Inner argument JSON is opaque to the transport and is
parsed only by the original normalizer, including its original duplicate-key
behavior. Original outputs containing big integers, non-finite numbers or excess
depth are explicit profile exclusions, never counted as successful semantic parity.

The successful result is the original result, not an error envelope. Original
normalization failures and transport exclusions become canonical typed INVALID.
Original exception class/code/message are retained in oracle evidence; the native
Core public error is independently sanitized to `provider reported invalid input`.
CoreStrictV1 is not relaxed or redefined.

## Reproduction and evidence

1. `prepare_ordered_tool_bridge.py --output <directory>` extracts and pins source.
2. `freeze_ordered_tool_bridge.py --output <oracle.json>` requires exact native
   CPython 3.13.5 / Unicode 15.1.0 and records real original outputs.
3. `stage_ordered_tool_bridge.py --output <new-directory>` creates a deterministic
   SDK build/3 plan without building or changing an active profile. Its ninth
   input, reviewed-inputs.json, pins the exact original oracle bytes, freeze
   script, protocol, source provenance and complete input case set using hashes
   only. Oracle outputs and freeze code are not staged inside the guest.
   Replay requires the full reviewed source manifest, manifest hash and plan hash,
   including the original source-to-provenance link and every adapter/codec/WIT
   input, rather than trusting a provenance hash alone.
4. Build only using separately verified producer tooling. Official componentize-py
   0.25.0's guest is CPython 3.14 / Unicode 16, not the original interpreter.
5. Replay the resulting typed component against the frozen original oracle. Guest
   runtime-info reports actual guest facts; it is not inferred from host Python.
   The reviewed custom producer target is CPython 3.13.16 / Unicode 15.1.0;
   the original reference remains CPython 3.13.5. Replay requires that exact
   reviewed guest pair AND exact original behavioral parity, not a minor-version
   match or an assumption that two patch versions behave identically.

The U+1C89 repr probe deliberately preserves original Unicode 15.1 expectations.
A newer guest's divergent result must remain a failing comparison. No historical
lost artifact, count or test run is evidence for this reconstructed candidate.
