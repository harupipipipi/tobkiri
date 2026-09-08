# Saved-turn bridge v2 implementation contract

Status: implementation plan, not a registered protocol or acceptance evidence.
The existing v1 `conversation.turn.v1/complete` remains supported and single-hop.
Do not remove its one-exchange guards to implement saved conversations.

Initial state machinery now exists in `tobkiri_host/continuation_chain.py`, but
is not connected to either VM boundary. It retains registered identities until
the original deadline, issues local single-use resume permits, bounds four hops
and cumulative encoded request/result bytes, and fences cancelled/failed chains.
It does not authenticate frames, grant execution authority, stop providers or
provide durable restart recovery. Versioned envelope validation and explicit
guest-side dispatch integration remain required before using it in production.

`tobkiri_host/continuation_session.py` couples the codecs and shared chain ledger:
it captures the bounded target plan, registers before returning the first frame,
consumes each verified reply before exposing one fresh set of child arguments,
and seals subsequent frames using the retained predecessor digest. Invalid
results or next steps fence the chain; cancellation and the original deadline
are rechecked before exposing resume arguments. Final outcome bytes count toward
the original cumulative budget. The real-owner four-step test now uses this
session. It still does not authenticate transport, authorize actions, dispatch
children, terminate execution or provide durable recovery. In particular, its
deadline must use the ledger's local clock: Host and guest monotonic clocks
cannot be assumed interchangeable when wiring the VM boundary.

`tobkiri_host/continuation_envelope.py` now defines strict v2 request/result
validation, still unconnected. Requests carry exactly `kind`, `version`,
`request_id`, `binding_digest`, `hop`, `nonce`, `previous_digest`, `target`,
`payload` and `state`. The expected identity/hop/predecessor/target are supplied
independently from authenticated state. Request bytes are limited to64KiB and
depth16. Replies carry exactly `kind`, `version`, `request_digest` and `outcome`,
with a512KiB/depth16 limit and exclusive `ok/value` or `error/error` variants.
The next predecessor is the canonical digest of the verified prior reply.
Decoded contents are retained as immutable canonical bytes. Duplicate keys,
invalid Unicode, non-finite values and excess depth/size are rejected by the
existing strict protocol parser. This validates format and binding only: an
`ok` transport outcome is not proof of a successful owner action, and an error
must not automatically advance the saved-turn application workflow.

## Observed boundaries

- `ecosystem/defaultspack/runtime/conversation.py` emits a bounded v1 AI request.
- The root guest runner validates that request, holds its continuation in
  `_PendingBridgeLedger`, and resumes a fresh sandboxed child once.
- `tobkiri_host/macos_vz_supervisor.py` independently validates signed frames,
  binds nonce/request/domain/artifact/deadline identities and requires a final
  outcome after `_complete_bridge`.
- `core_runtime/bootstrap/production_v4.py` selects the exact captured nested
  edge, rechecks activation/authority and invokes its Broker-bound provider.
- Conversation and message state belong to `rumi_conversation_store_pack`.
  Its captured readers/actions use an explicit Profile/root and owner revisions.
  Registration of message actions has not granted any new live Profile edge.

## Required saved-turn sequence

The pure application computation now exists in
`ecosystem/defaultspack/runtime/saved_conversation.py`. Its `saved_complete`
ABI accepts either `{request}` or root-created `{state, outcome}` and emits
`tobkiri.packvm.continuation.intent.v2` containing `hop`, `target`, `payload`
and `state`. These intents are deliberately **not registered or connected** to
the v1 guest runner. They must never be treated as terminal success by a v1
wrapper. The v2 root integration must add request/binding identity, nonce and
predecessor from retained authenticated state, independently validate the fixed
target sequence, and consume the result before resuming a fresh sandbox child.
External callers must not be allowed to submit resume state.

The saved-turn file is now explicitly digest-pinned as an executable artifact
in the canonical Pack source, artifact index and generated bundle. It has no
Function variant or published operation yet. Sealing these source bytes fixes
the integrity scanner's unlisted-runtime-file error; it does not make the
unfinished saved-turn dispatcher available or grant a live Profile binding.

`continuation_envelope.seal_continuation_intent` now converts that exact intent
shape into a validated request using independently supplied root identity, hop,
target, nonce and predecessor. Extra application-supplied framing fields are
rejected. The four-step owner test uses this implementation instead of creating
request frames itself. This is not yet guest-root installation or dispatch.
Both the v1 guest wrapper and the Host final-outcome validator now reject the
reserved `tobkiri.packvm.*` namespace as terminal application data; an unsupported
v2 intent cannot masquerade as a completed v1 invocation. The existing initial
v1 bridge request still follows its dedicated validation path.

The packaging path now uses `scripts/build_packvm_guest_bundle.py` to produce
a deterministic, uncompressed zipapp. Its exact closure is the existing runner
as `__main__.py`, the three continuation modules, bounded child pipe I/O,
protocol canonicalization/errors,
and empty package initializers. No Host dispatcher, state owner, credentials or
third-party dependencies are included. The pipe helper is now used by the actual
initial and resumed child execution path: stdout is bounded while reading,
stderr is counted but never retained (64KiB maximum), and concurrent nonblocking
stdin/stdout/stderr I/O shares one 60-second step deadline, including waiting
after pipe EOF. On exchange or serialization failure the runner requests process
group termination, closes pipes and reaps without an unbounded `communicate()`.
Termination errors are not suppressed. Real Host subprocess tests cover pipe
pressure, flooding, timeouts and reaping with a direct-child stop adapter; they
do not certify Linux guest process-group termination. Initial and resumed child
registration failures now use the same stop/close/bounded-wait cleanup, including
interruptions outside `Exception`. They never drain artifact output with
`communicate()` before registration. Tests exercise both entrypoints with a real
child flooding stderr and verify closed pipes and a reaped direct child. This
does not claim recovery of a partially written request registration record.
The authenticated guest
dispatcher now captures one guest-local deadline before initial execution and
retains it through the pending Host exchange and resumed child. Artifact checking,
pipe exchange (including the absolute deadline), and late result observation use
the remaining budget; registration cannot reset the pending TTL after initial
computation. Host wait consumes this same guest budget. The Host's original
monotonic value remains unchanged in the signed bridge and is enforced by the
Host independently: its clock origin is not assumed equal to the guest's. This
is connected v1 deadline handling, not a registered v2 multi-hop dispatch path.
The parent also applies the shared strict JSON parser directly to received
child stdout, before dictionary normalization or bridge validation. Duplicate
keys, invalid Unicode, non-finite/floating-point numbers, unsafe integers and
excessive depth are rejected with a fixed error that does not expose parser
diagnostics. Actual-pipe tests cover both ordinary excessive depth and parser
recursion overflow. This closes a byte-validation gap in both initial and
resumed execution; it does not register or connect the v2 transport.
`build_packvm_vz_helper.sh` stages this
archive before binding the existing guest-runner digest and service template;
the existing signed provisioning manifest covers all archive bytes. The root
runner binds the archive itself (not the virtual `__main__.py` path) into each
fresh child sandbox. Raw source-runner execution remains available for tests.

Isolated `python -I -S` doctor and continuation imports from the archive are
tested, as are exact member identity, deterministic bytes, changed-archive
rejection and linked-source/output refusal. This provides the guest dependency
delivery path but does not install it into the currently running VM or implement
multi-hop dispatch. The native app must be rebuilt and its normal verified
provisioning/launch workflow followed; do not replace live guest files directly.

The computation now reads the exact displayed revision, derives stable message
IDs from conversation/turn/role, follows the selected message ancestry, preserves
the owner's model reference, checks exact append acknowledgements, and uses
owner-returned revisions. Lost writes return unknown persistence and a
reconciliation requirement, never a retry intent. Existing stable IDs require
reconciliation even when the caller supplies a newer revision. AI intent size is
checked before the user write. Oversized/failed AI output leaves the acknowledged
user message saved without pretending an assistant message was written.

Current input is nonempty text. System-prompt/agent references and history
requiring tool/parts/widget resolution stop explicitly before a user write;
they are not silently discarded. Their captured context resolution, real
provider readiness preflight, durable coordinator/reconciliation, protocol
registration and root-guest/Host dispatch remain required. Isolated tests use
the real conversation owner and v2 codecs/chain ledger, but a controlled AI
result; this is neither actual AI nor native/full-UI acceptance.

Use a distinct, explicitly registered saved-turn operation and versioned bridge
envelope. Its initial request includes a stable turn ID, conversation ID, the
displayed conversation revision and user input. It must not accept caller-selected
Profile/root, authority receipts, credentials, target bindings or continuation state.

The shared protocol library now defines `saved_conversation_input` (file
`saved_conversation_input_v1.schema.json`) for the initial `{request}` ABI.
It permits only stable turn/conversation IDs, a positive I-JSON-safe displayed
revision, and nonblank text. Additional fields at either level are rejected,
including model overrides, caller-selected targets and guest resume state.
Normal text may mention authority words without becoming an authority field.
The existing computation separately enforces its encoded request/intent budget;
JSON Schema character constraints are not a substitute for that byte limit.
Tests compare the schema with the pure initial ABI and explicitly reject the
internal `{state, outcome}` shape at this external boundary. This is a schema
definition, not Function publication, route wiring or a live Profile grant.
Production saved-send must call this validation before its first dispatch when
the versioned transport and captured operation are connected.

| Step | Captured target | Required outcome before advancing |
| --- | --- | --- |
| 0 | Conversation resource/get | Existing record; requested revision matches |
| 1 | Message action/append user | Stable message ID persisted at that revision |
| 2 | AI generation/stream | Selected model reference and owned message context; real provider outcome |
| 3 | Message action/append assistant | Stable assistant ID persisted at the returned owner revision |

Four exchanges are sufficient for this baseline, not an unbounded tool loop.
Tool execution requires its own existing approval/runtime path and explicit
extension; do not treat model tool intents as automatically executable.
Preflight the required captured bindings/backend readiness before the first
write, without claiming that preflight guarantees later provider availability.

## Continuation invariants

1. Preserve the original request identity, caller, domain, activation, artifact
   identity and deadline through every exchange. A new hop cannot renew the turn.
   The current v1 Host bridge now forwards the authenticated outer monotonic
   deadline through `V4DispatchSession.invoke` to `RequestBroker.invoke` as a
   Host-only ceiling. The Broker rejects invalid/expired ceilings before
   preparation and uses the earlier of its own operation deadline and the
   parent's original deadline. No application payload supplies this ceiling.
   This is connected v1 deadline propagation, not v2 multi-hop integration or
   proof that every provider honors cancellation. Both VM bridge dispatch and
   Host Provider nested contract clients now forward a Host-created shared
   cancellation Event as well as that deadline. Neither is serialized into
   application payloads or accepted from guest JSON.
   The Broker also checks the deadline after receiving a completed Future:
   `Future.result(timeout=0)` alone accepts an already-finished late result.
   Late observations enter the existing cancellation/error path; potential
   external effects remain ambiguous and require reconciliation.
2. Keep the current hop and predecessor digest in the root guest ledger. Consume
   a continuation before resuming; advance through an explicit method rather
   than reusing initial registration with a fresh TTL.
3. Each hop gets a new nonce and a digest covering its exact target, request,
   version and continuation state. Both guest runner and Host validate the same
   versioned shape; mixed-v1/v2 frames fail closed.
4. Continuation state is bounded application data, never authority. Profile,
   resource scope, caller and provider selection still come from captured
   bindings and Broker checks. Do not duplicate a permissive direct invocation
   path in HTTP normalization or use the legacy ambient storage factories.
5. Owner-returned revisions, not guest-invented increment assumptions, determine
   the next write. Stable message IDs are derived once from the original turn,
   not regenerated after a transport error or restart.
6. Enforce limits on exchanges, cumulative state/result bytes, outstanding
   continuations and total elapsed time, not just each individual frame.

## Failure, restart and cancellation

- A stale initial revision writes nothing. A later failure may leave the user
  message saved; report that partial state, never an assistant success.
- A timeout or lost write result is an uncertain outcome, not permission to
  repeat the write with a new ID/revision. Reconcile through the owner using
  stable IDs; conflict is not automatically equivalent to successful completion.
- `rumi_turn_runtime_pack` is the existing lifecycle/status owner. The new
  `runtime/durable.py` provides explicit-root SQLite persistence around its
  existing lifecycle semantics. Begin requests require stable IDs and reject
  rebinding; independent connections/processes use a write transaction plus an
  exact revision. Terminal identities are retained: capacity exhaustion rejects
  new turns instead of pruning replay protection. Reads do not create storage,
  and oversized mutations roll back without changing the record. The existing
  lifecycle/resource/event contracts now register `runtime/host.py`, capturing
  one exact Profile/root/domain and sharing that durable owner. Read contracts
  expose snapshots only and cannot mutate lifecycle state. No live Profile was
  activated by this source registration. There is no restart coordinator yet. A
  recovered `running` record is only a snapshot, never permission to re-execute
  AI or an uncertain write. Implement reconciliation and captured execution
  integration before
  exposing saved-send as complete. The legacy `TurnRuntime` and its global pool
  remain in-process and still prune terminal mappings. Do not put execution
  state into the conversation storage Pack.
  The durable begin operation now accepts an optional `input_digest` binding.
  When present, it survives transitions and process restart and cannot be
  changed, omitted on replay, or retroactively attached to a legacy record.
  Malformed digests are rejected before creating storage. The future saved-turn
  coordinator must compute this digest from the validated complete initial
  input (including text) before begin; the lifecycle owner only retains the
  supplied identity and cannot verify absent input bytes. A digest is neither
  an approval credential nor an execution/retry permission. Legacy callers
  without this binding retain their existing behavior, so this optional field
  does not yet close saved-send's mandatory input-binding requirement.
  The captured lifecycle contract also now exposes `begin_saved`, accepting
  only the validated `{request}` initial shape. It checks the 60 KiB UTF-8
  request budget before creating storage, derives a stable request ID from
  the captured Profile and turn ID, and computes the complete initial-input
  digest itself. Callers cannot supply those derived fields. Changed text with
  the same IDs conflicts; identical input returns the existing queued/running/
  terminal snapshot without changing events or starting computation. Read-only
  turn contracts cannot call it. This prepares durable identity only: it does
  not read conversation state, preflight providers, claim execution, run the
  guest, or store a message. The saved-send coordinator still must use this
  operation through its captured Broker edge and then handle reconciliation.
- Cancellation must reach the currently executing nested Broker request as
  well as the guest continuation. Dropping an HTTP response or cancelling only
  a pending guest nonce does not prove provider execution stopped.
  The current v1 VZ supervisor now marks an active request as cancellation
  requested before sending the guest cancel frame. It checks that local fence
  before calling the Host bridge, after its callback returns, and before
  publishing a signed guest result. A lost cancellation acknowledgement does
  not clear the fence. This prevents observed late results from resuming or
  succeeding. The same signal now reaches each nested Broker invocation, whose
  bounded wait requests cancellation using that invocation's own request ID.
  Already-cancelled parents cannot start nested work. Local calls report
  `cancellation_requested`, not confirmed termination; failed backend cancel
  requests report a provider error, and external effects remain ambiguous.
  Cancellation/timeout does not release admission or request materialization
  while the provider Future is still running; cleanup is deferred until its
  actual completion. Broker close remains non-blocking and is not a completion
  signal. This does not certify every backend's termination, establish
  cross-process cancellation ordering, or replace the guest's cancellation
  ledger. A durable saved-turn coordinator and authenticated UI stop operation
  still need to connect user intent to the correct live request.
- After cancellation, a late result must not start another hop. Distinguish
  cancellation requested, confirmed termination, uncertain completion and
  already-persisted effects. Never erase saved data to simulate rollback.
- Streaming requires actual incremental events with request identity and
  ordered sequence, and a terminal persisted/error/cancelled state. Wrapping a
  completed result in SSE, or replaying a buffered list, is not streaming proof.

## Credentialed HTTP boundary

The Host credential transport retains the authenticated request envelope.
Its HTTP budget is bounded by both the supplied wall-clock deadline and the
original Host monotonic deadline; a payload cannot extend the latter. Before
resolving material, immediately before opening the request, and after consuming
the response, it checks the captured cancellation/deadline and durable authority.
A cancelled or late response is denied rather than audited as completed. Material
cleanup and single-use consumption remain in place.

These checks do not interrupt an already-blocked socket or undo a remote effect.
In-flight I/O still needs provider-specific termination/reconciliation; Broker
resource charges remain retained until the provider Future actually finishes.
This transport fence is not a UI stop operation or streaming implementation.

## Implementation and acceptance order

1. Define and test versioned bounded envelopes and root-owned chain state;
   preserve all v1 tests. Test replay, skipped/reordered hops, cross-request or
   cross-domain frames, state tampering, expiry and cancellation at each step.
2. Wire both guest runner and Host supervisor together, then the captured nested
   dispatch. Test the actual producer through both validators and the real
   Broker; separate mocked provider behavior from real-provider acceptance.
3. Implement the saved-turn application operation and durable status/restart
   ownership, then explicitly register artifacts, schemas and Profile edges.
   The new active composition requires normal review; never modify live state
   as part of source generation.
4. Expose fixed full-UI send/status/stream/stop routes and update the client.
   Test interrupted writes and late results against real isolated owner storage.
5. Build and review the new composition, then verify a real conversation and
   native stop/approval behavior on existing Defaults `/chat`. Source tests do
   not close the native, PackVM, Wasm, lifecycle, CI or DMG TODO requirements.
