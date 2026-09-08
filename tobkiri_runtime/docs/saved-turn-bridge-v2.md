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
guest-side packaging/integration remain required before using it in production.

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

Use a distinct, explicitly registered saved-turn operation and versioned bridge
envelope. Its initial request includes a stable turn ID, conversation ID, the
displayed conversation revision and user input. It must not accept caller-selected
Profile/root, authority receipts, credentials, target bindings or continuation state.

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
  and oversized mutations roll back without changing the record. This wrapper
  is NOT yet a captured Host provider or restart execution coordinator. A
  recovered `running` record is only a snapshot, never permission to re-execute
  AI or an uncertain write. Register the lifecycle, resource and event contracts
  against the same captured durable owner, then implement reconciliation before
  exposing saved-send as complete. The legacy `TurnRuntime` and its global pool
  remain in-process and still prune terminal mappings. Do not put execution
  state into the conversation storage Pack.
- Cancellation must reach the currently executing nested Broker request as
  well as the guest continuation. Dropping an HTTP response or cancelling only
  a pending guest nonce does not prove provider execution stopped.
- After cancellation, a late result must not start another hop. Distinguish
  cancellation requested, confirmed termination, uncertain completion and
  already-persisted effects. Never erase saved data to simulate rollback.
- Streaming requires actual incremental events with request identity and
  ordered sequence, and a terminal persisted/error/cancelled state. Wrapping a
  completed result in SSE, or replaying a buffered list, is not streaming proof.

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
