# Current-source finalization development trial

This directory preserves actual isolated custom-Python build and development
replay evidence from 2026-10-20. It contains no component binaries, expected-output
stubs, production activation, or native Core/Gate acceptance. Archiving these
records did not execute a producer or guest.

## Preserved evidence

- `builds/` contains six successful build receipts: bridge v1, bridge v2,
  original Gateway tool caller, usage-cost provider, stream normalizer, and
  finalizer. Each has its exact source plan, producer driver, generated outer
  WAC, supervision, complete mounted-input inventory, and unchanged-input
  result. The build receipts bind the actual component hashes and sizes.
- `replays/bridge-v1/` records all 33 cases: 24 admitted cases and nine enforced
  exclusions. `replays/bridge-v2/` records all 33 cases: 30 admitted cases and
  three verified nonfinite boundaries. Both observed CPython 3.13.16 and Unicode
  15.1.0. They are standalone development replays, not native acceptance.
- `replays/five-component/` records the actual bridge-to-caller and
  caller/usage/stream-to-finalizer composition. All ten cases matched the source
  reference or declared finite-domain boundary: five finalized responses, three
  typed Core-error mappings, and two domain rejections. All five runtime-info
  results, lifecycle calls, operation returns, and canonical post-returns passed.
- `source/` contains the exact source-only oracles and captured-source hash
  inventories. The original reference runtime is CPython 3.13.5, Unicode 15.1.0.
  Reference observations alone are not evidence of guest execution.
- `failed-attempts/finalizer-stream-keyword/` preserves the first failed
  finalizer bindings attempt, including its original WIT and complete diagnostic
  log. Unescaped `stream` was rejected as a reserved WIT keyword. The successful
  finalizer uses `%stream`, retaining the exported operation name `stream`.
  The failed attempt produced no component build receipt and is not relabeled
  as a successful build.

The five-component artifact was 89,516,870 bytes, SHA-256
`e79b9419a9ac665f53399f4946d17b2e4330d43b5f7e5df4feb9739174d5f58e`.
Its exact five input identities are in `replays/five-component/pins.json`.
`composition.wac` is the exact `WAC_SOURCE` bytes literal extracted without
execution from the captured runner; its hash matches both pins and replay.
Each replay preserves its executed driver and runner. Fixed sandbox paths in
those archived scripts are evidence, not a portable invocation recipe.

The five-component replay completed under external supervision in 93.67 seconds,
with sampled peak RSS 1,936,957,440 bytes, all 1,656 mounted input records unchanged,
and no known surviving processes. Standalone bridge replays took 15.26 and
15.46 seconds, with 1,593 and 1,596 input records unchanged. The supervisor retains
an 8 GiB per-process address-space limit, a 1,200-second process timeout, and
sampled RSS/process guards. Sampling is not a hard RSS or PID cap.

The composed development fixture uses a 128 MiB initial memory reservation,
320 core instances, and eight memories. The 128 MiB Store memory limit is per
linear memory, not an aggregate process limit. These are bounded fixture
allowances, not measured minima or changes to production defaults. Fuel remains
two billion per whole case, with a 30-second whole-case epoch deadline; compilation
is bounded by the external supervisor rather than guest fuel or epoch timers.

## Identity and proof limits

`archive-index.json` records every archived file's SHA-256 and length, its
copy/extraction origin, all six component identities, and the composition pins.
It covers every file except the index itself. Copied files were byte-compared
with the actual local trial outputs; the WAC extraction was independently
hash-checked. Producer startup provenance remains in
`../2026-10-19/producer-receipt.json`. This is the preserved custom producer,
not an official-wheel SDK compiler identity.

The existing `source-rebinding.json` is unchanged. The passing historical
checkpoint `92f38e3241b802605a4af59ba35aae72ee73cdbd` and earlier trial directories
remain preserved. Their receipts are historical proof, not current-source proof.
Both bridge corpora retain all original 33 case observations.

Native Core/Gate acceptance, production activation, live provider generation,
retry parity, billing authority, full Host/Gateway migration, all typed error-arm
execution, dynamic guest import-call counts, and partial stream mutation
observations remain **false / not established**. The residual five Core imports
trap in this development composition. Successful expected boundary results do
not establish original HTTP error bytes, production retry/billing behavior, or
independent Pack CodeOwner/Gate routing.
