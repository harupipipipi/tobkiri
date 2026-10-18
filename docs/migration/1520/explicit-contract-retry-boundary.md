# Explicit contract-retry boundary

A malformed completed response can consume most of an invocation's deadline.
Automatically starting its correction in that invocation risks leaving the
next attempt pending when the deadline expires. The DeepThink Pack returns
control after an eligible rejection and requires another explicit continuation.
Core remains generic; no domain-specific Core state or operation is added.

## Saved transitions

1. Retain the whole rejected response, request identity, attempt and real usage.
   The rejection settlement and its snapshot share the existing transaction.
2. If the captured correction bound allows it and no same-phase tool effects
   make retry unsafe, prepare the next generation epoch, retry context and
   counter. Save a second confirmed snapshot with status `awaiting_retry`.
3. Only after confirmation, emit usage, progress and an `awaiting_retry` event,
   then EOF. There is no answer, `done`, approved candidate or new completed
   domain phase. Conversation storage treats this separate receipt as a no-op
   before opening its Store. The UI retains the existing conversation and
   offers a manual Resume action.
4. A separate `local.deepthink.continuation-input@1.0.submit` selects the same
   Run and conversation with `answers: null`. The worker validates the saved
   preparation, pinned model/settings, completed rejected attempt, unchanged
   phase entry, retry counter and unused successor identity. It resumes without
   incrementing the prepared epoch or counter again. Normal head CAS, unique
   SQL claim and the nonserialized dispatch permit still precede generation.

The receipt describes preparation; it is not a dispatch permit. Status reads,
implicit workflow calls and matching stored claim rows cannot mint authority.
The existing correction limit is not reset by continuation. A successful
continuation still completes at most one domain phase.

## Failure and compatibility

- Pending model attempts, uncertain tool effects and unknown checkpoint commits
  require reconciliation. They cannot be relabeled as a saved retry boundary.
  A failed preparation write exposes no boundary, makes no replacement write
  and starts no successor in that invocation.
- A quarantined schema rejection without confirmed preparation, including an
  older rejected-only snapshot or exhausted retries, is refused by public
  continuation. Installing a new Pack does not migrate these into permission
  to send another request.
- Strict JSON parsing, phase schema validation and semantic review are
  unchanged. Trailing text is rejected rather than stripped. Saved original
  responses and billing records remain available.
- Missing usage/cost remains unknown. A complete response with incomplete
  accounting does not gain zero-valued usage from suspension.
- Existing `paused` receipts continue to require completed phases. The new
  boundary has its own closed schema and may have zero completed phases.
  Existing Harness `done` conversation behavior is unchanged.
- Original in-flight deadlines are not extended. A separately requested
  invocation receives its normal request context. Provider output-token
  settings and HTTP compiler behavior are unchanged.

## Verification scope

Deterministic worker tests cover no successor claim before explicit resume,
single use of the prepared epoch, bounded exhaustion, pinned-binding mismatch,
malformed preparation, pending attempts, unknown preparation saves, tool-effect
exclusion, stream EOF and missing accounting. Store tests check no conversation
append and rejection of mixed/invalid receipts. UI tests cover manual resume
and history preservation. Semantic-review fixtures retain original candidates,
reviews, rejected generations and accounting across correction boundaries.

These tests do not establish that a provider will obey JSON, that a semantic
review is correct, or that an already-pending live request can be recovered.
Real Core/guest, SQL, host and provider evidence must be reported separately.
