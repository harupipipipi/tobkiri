# Current-source two-component development replay

This archive preserves the successful 2026-10-20 replay of the freshly built
original-Python Gateway tool caller linked to the original-Python ordered bridge
v2. It is separate from the historical
[`2026-10-20-gateway`](../2026-10-20-gateway/) trial. No historical artifact pin,
oracle, runner or trial receipt was replaced.

## Observed result and limits of the claim

[`replay.json`](replay.json) has SHA-256
`be47ef5b7553b21d773120823870c7dc4a1663cd7f01b7ad1b7eca8f72eee828`.
It records:

- All 30 ordered source cases matched: 16 exact successful JSON strings,
  seven `invalid` boundary results and seven `internal` nonfinite-output
  boundary results, including exact error messages.
- Both guests reported Python 3.13.16 / Unicode 15.1.0 in every case:
  60 runtime records in total.
- All 210 explicit calls returned in the expected order, including both guests'
  init and shutdown in each case. Every case completed without a retained
  runner failure.
- Canonical post-return cleanup succeeded through the unchanged `_invoke`
  cleanup path and failure-free completed cases. The receipt does **not**
  contain a separate list of 210 post-return events.
- The composed component had exactly the reviewed recursive typed surface.
  The five residual canonical Core imports were trap-only; the caller's bridge
  import was satisfied by the actual provider component, not a host success
  substitute.

The source oracle was observed under exact CPython 3.13.5 / Unicode 15.1.0.
Its 30 case observations are unchanged from the historical case observations;
only source-binding and provider-oracle provenance changed. Invalid/nonfinite
transport mappings are not claims of full Host or legacy HTTP error parity.

This is **development composition evidence**, not native Core/PackHost or Gate
acceptance, production enablement, caller activation, billing authority, a
full-Gateway migration, retry parity, all-error-arm coverage, observed internal
import-call counts, or observed partial stream mutations. Those flags remain
false in the receipt. No component build was performed during this replay.

## Exact inputs and execution boundary

[`pins.json`](pins.json), SHA-256
`d8ed300d82dd36e2bf09cd2988850fb0a2291d604b37da0fc72a48345f91c916`,
binds both actual component/receipt pairs, the source oracle, WAC executable,
fixed graph and 16-module runner/helper closure. Both build receipts were checked
against freshly reconstructed current source plans, not historical manifests.

The independently reviewed runner identity is
`f3f859a47aa16c45a1c0dc0ee5c8ec19f853fb162b973eb25f4c96066897e2bd`;
the full helper closure identity is
`3c4d619cd758dee5405d4fa6b4f3697cc065ad0e764409c7b276085f05d22e18`.
[`source-capture.json`](source-capture.json) binds all 83 captured source files
(1,360,136 bytes). The host-only oracle does not enter either guest build plan.

The exact two-component graph is [`composition.wac`](composition.wac), SHA-256
`107675f2bfcdbe32bd7eab4c77a9aa9bde2e6dc2e6d1aa9378989a006f2ba4b4`.
The resulting component was 35,800,659 bytes, SHA-256
`c00209ee4863a4d064c4ff994ead86cd644e284b657e048420b3cd247788c87d`.
Neither component binaries nor the composed binary are included here.

The fixed driver used the frozen source tree, explicit dependencies and an absent
ambient dependency directory. The networkless launcher used read-only input
mounts, an isolated writable work directory, cleared environment and private
process namespace/group supervision. Its complete mounted-input inventory has
1,651 records; the before/after check reports all unchanged.

Limits remain those of the earlier two-producer development fixture:

- 128 MiB maximum per linear memory and 128 MiB virtual reservation;
  four memories, 64 tables, 65,536 table elements and 128 instances.
- Two billion fuel per case, with a 30-second whole-case epoch deadline beginning
  before instantiation and covering lifecycle, runtime and operation calls.
- External supervision: 8 GiB address-space limit per process, 20-minute
  wall timeout, sampled 3 GiB aggregate RSS / 64-process thresholds and
  unchanged host memory/disk floors.

The supervisor recorded success in 37.2688489630018 seconds, peak sampled RSS
836,554,752 bytes, at most four observed processes and no remaining known
processes. RSS/process sampling is not a hard containment limit. Guest fuel and
epoch interruption do not bound WAC or native compilation.

## Archive contents

The following are exact copies of trial evidence, not regenerated reports:

- `replay.json`, `source-observations.json`, `pins.json`
- `caller.component.build.json`, `provider.component.build.json`
- `replay-trial.py`, `replay.supervision.json`
- `replay.input-inventory.json`, `replay.inputs-unchanged.json`
- `capture.json`, `source-capture.json`

`composition.wac` is the exact byte literal from the frozen reviewed helper,
verified against the graph pin. `supervise.py` preserves the external controller
source for inspection; it is not a guest-mounted input. Machine-specific host
launch wrappers and binaries are intentionally excluded.

[`receipt-index.json`](receipt-index.json) records each archived file's size and
SHA-256. This README and the index explain existing evidence; neither represents
a new build or replay. The archive contains no internal review notes or private
execution identifiers.
