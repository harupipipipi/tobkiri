# Durable DeepThink Runs and explicit generation recovery

DeepThink keeps its `deepthink.workflow.v5` request identity, captured Settings,
model, original messages and provider/tool pins. New Runs use a separate
`deepthink.run-ledger.v1` SQL repository in the Pack's existing private
`workflows` store. Atomic KV replacement is no longer its Run authority.

## Saving and dispatching

The repository uses the ordinary `storage@1.1.0` transaction resource. A Run
head contains its original request digest, monotonically increasing revision
and exact snapshot digest. Every state transition appends a snapshot and
conditionally advances that head in one transaction. A stale head rolls back.

Before every model request, including contract retries and tool rounds, the
Pack inserts a unique claim bound to the exact Run, phase, generation key,
attempt, prompt/schema request and captured route. Only the invocation whose
new insertion received a confirmed commit can hold the in-memory dispatch
permit. The permit is consumed once, against its exact claim and committed
head, immediately before generation. It is never serialized or reconstructed
from a matching saved row. A crash or unknown commit therefore cannot remint
permission to send a request. This guarantees at most one local dispatch per
claim; it does not promise exactly-once remote execution.

Public progress has a separate sequence/digest CAS. Latest payload chunks can
be replaced transactionally; stamp history is append-only. Unchanged chunks
are reused. A resolution compares the current progress stamp in the same
transaction as its immutable receipt and Run-head update. A late writer cannot
overwrite resolved text, identity or usage. Unknown commits stop the current
invocation without an automatic write or model retry.

Public text deltas are coalesced before SQL persistence: 64 accepted delta
records or 4096 UTF-8 bytes trigger a flush, whichever comes first. Generation
identity changes, usage, completion, read/validation errors and EOF flush the
accepted prefix. The initial empty progress record is confirmed before the
provider call. A failed save poisons the recorder and permits no retry or next
pull. Snapshot sequences count actual saves, rather than received events.

At a normal event boundary the unflushed tail is below both thresholds. One
incoming event can cross a threshold while its synchronous save is in flight.
A process crash or deadline can lose that unconfirmed tail; an inactive Store
cannot guarantee its final flush. The last confirmed prefix and pending claim
remain authoritative and do not authorize an automatic replacement request.
Frequent public heartbeat boundaries can increase the number of saves.

Each progress save reads its head, exact claim, resolution blocker and current
progress dimensions in one bounded typed join inside the same transaction.
Chunk metadata remains a separate bounded read. All identity, sequence, digest,
resolution and affected-row checks remain in place. For one changed chunk this
reduces the existing-progress path from eleven Store calls to seven (the first
save from ten to seven). These are source-derived call counts, not a guarantee
that a complete model phase finishes within its unchanged deadline.

Opaque chunks use canonical lowercase HEX TEXT through typed SQL. This avoids
the existing Core BLOB integer-array conversion path without modifying its
allocator, Gate or limits. Raw bytes remain the hash input. A 64 KiB raw chunk
occupies at most 128 KiB of SQL text; four-chunk query batches stay below the
existing 4 MiB output bound. Records are bounded at 8 MiB, and the existing
64 MiB database quota still applies. HEX requires twice the raw storage.

Readable `goal.json`, `goal.md` and candidate/answer KV artifacts are projections.
Their failure is reported separately; it cannot erase a confirmed SQL Run.
Managed Runs never fall back to an older KV checkpoint or sidecar after a
missing, corrupt or unavailable SQL record.

## Inspect, reconcile, regenerate

The normal User-facing `local.deepthink.continuation-input@1.0` port provides:

- `status(run-id)`: the retained Run, usage, attempts and current repository head.
- `reconcile-generation(selection)`: inspect one original attempt through the
  configured ordinary generation-reader and commit an immutable resolution.
- `regenerate-phase(selection, resolution-digest)`: an explicit new generation
  through the Pack-owned regeneration Flow, with a unique successor claim.

The selection contains `run-id`, `conversation`, `attempt-id`,
`expected-revision` (canonical decimal string) and `expected-snapshot-digest`.
Use the exact head returned by status. A changed head, conversation, Settings,
provider route or progress stamp rejects the operation.

The User never supplies trusted usage, terminal metadata or an approval flag.
The configured reader's `lookup-requested` first fetches the public fixed
OpenRouter model catalog without credentials, checks its exact requested ID
and canonical slug, and records the bounded raw response digest. It then uses
its own opaque Secret reference for generation metadata and optional stored
public completion. These are GETs outside a SQL transaction. Unknown aliases,
identity mismatches, missing terminal accounting and uncertain local/tool
custody do not authorize another request. Catalog evidence describes the
current mapping and does not prove that an alias never changed historically.

An identity-matched terminal generation with unavailable full public text is
recorded as `terminal_content_unavailable`. Its original attempt, prefix,
sequence and accounting remain frozen. Ordinary continuation cannot replace
it. Explicit regeneration creates a different generation epoch and one unique
successor authorization. Earlier tool effects are retained and cannot be
replayed by this recovery action.

Recovered full text must match the exact saved prefix. It enters the existing
phase schema/semantic validation and review path; the lookup itself completes
no phase and executes no tools. Invalid recovered text is quarantined normally.
Accounting is projected once per original attempt, independently of whether
its text was recovered or discarded. Missing usage remains unknown, and exact
decimal costs and large counters are not rounded through floating point.

Core audit retains the actual dispatch binding and security epoch. The guest
API does not expose all binding/Profile introspection; unavailable provenance
fields are explicitly null rather than copied from an unrelated current pin.

## Stream errors and cleanup custody

Only the exact admitted Core stream-source result may supply a closed public
error code. Its diagnostic message is discarded. Traps, malformed results and
conversion faults remain internal errors; a cleanup failure cannot turn into
ordinary provider unavailability. Existing native and already-facaded no-replay
metadata survive later delivery/cleanup failures without promoting a facaded
code into native commit authority.

After a failed pull or terminal EOF, Core owns cleanup, including any retained
unknown receipt. The guest preserves the prefix, failure and pending claim and
does not issue a second void cancel against a possibly consumed handle. This
is no proof that physical cleanup completed. Local active, parse and progress
save failures still cancel their open stream through the ordinary Core path.

## Existing KV Runs

Legacy checkpoints and sidecars remain available for diagnostics. Public
automatic adoption is disabled. A Profile epoch change or `Session.close`
alone does not prove that every already-admitted old worker has physically
terminated. A User-provided `quiesced` flag cannot establish that fact.

The generic repository has a separately validated legacy-import primitive,
but the product does not expose it as a recovery shortcut. A host-owned
retirement capability is still required for general live migration. Evidence
that a particular old standalone CLI process was joined remains a limited
operator observation; it is not generalized into guest authority.

## Portable preparation and validation

`packs/recovery/build_deepthink_ledger.py` stages only the new DeepThink
component from a frozen Profile's original configuration and resource caps.
It copies every other Pack archive unchanged and replaces/adds only DeepThink
and the generation-reader. Output Profile sources are relative `file:packs/`
references. Preparation neither reads DATA/Vault nor applies a Profile,
approves permissions, starts a Session or calls a model. Use ordinary Core
Profile create/apply and its approval path separately.

The ordinary MiMo/Profile builder includes the actual generation-reader Pack,
its defining shape, binding and intersected Pack limits. This adds no direct
User Reader permission. Reader initialization makes no network request and does
not open the opaque credential. An optional consumed port still needs its
contract-owner shape in the selected Profile; copying a WIT dependency alone
does not publish that shape. A deliberately offline fixture can select a
same-owner Data-only contract variant with no provider, imports or effect
authority. It must not select that variant alongside the actual Reader Pack.

Domain/repository unit tests, real native Store/Gate transaction tests,
component/WIT admission and actual User/Flow/Wasm execution prove different
boundaries. A synthetic provider's success does not establish MiMo completion.
The actual MiMo v5 Run's first phase completed and its next generation's
terminal accounting was inspected, but its legacy Run has not been migrated
or accepted as a completed workflow. A later SQL-backed MiMo Run reached the
unchanged 300-second deadline in its first phase. A restarted typed status kept
its 2,025-byte prefix and generation ID. Ordinary User reconciliation and a
second restarted status then confirmed 3,067 native tokens and exact USD
`0.00059332` in that same SQL Run, with no new model POST. Provider metadata
reported normal completion, but public content returned 404. No phase was
accepted and no replacement request was automatically dispatched. This is
retention/accounting evidence, not a completed MiMo workflow or validation of
every later fix. Terminal-Bench remains pending acceptance.
