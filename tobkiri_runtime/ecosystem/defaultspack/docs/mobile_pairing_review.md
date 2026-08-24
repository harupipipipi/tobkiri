# Mobile pairing review

The desktop pairing review is a safety surface over the authoritative local
pairing record. Closing the review never approves, rejects, or cancels a
request. The close confirmation offers these distinct outcomes:

- **Keep pending and close** dismisses only the local review surface.
- **Reject request** records `rejected by desktop reviewer` through the
  pairing reject route.
- **Cancel pairing** uses the same protocol-backed reject transition with the
  distinct reason `pairing cancelled by desktop reviewer`.

Approve, reject, and cancel are available only after the current pairing ID
has a matching authoritative status and review response. A polling failure
puts the surface into a degraded state, disables every decision, and offers
Retry. A request remains pending until the backend records a terminal state.

## Authority and settlement

The frontend is not an authority source. It submits the reviewed claim hash
and requested scopes, then reads status again. Only an authoritative terminal
status (`approved`, `rejected`, `expired`, or `revoked`) produces the durable
settlement announcement. Pairing expiry is normalized and persisted by the
pairing domain before status or review is returned.

Approval also requires the active verified Pack v4 Profile context before the
pairing record can transition. Profile validation, device token issuance, and
encrypted pickup remain backend-owned; the review UI never receives or stores
pairing codes, pickup secrets, or issued tokens.

## Concurrency and recovery

Only one decision may be in flight. Dismissal and duplicate decisions are
blocked until it settles. Refreshes and decisions are generation-bound so
late responses after unmount, pairing-ID replacement, or reopening cannot
settle the current review. Reopening always starts with a fresh authoritative
status and review read. Explicit dismissal returns focus to the pairing-ID
input, and terminal results are announced through a polite live region before
the review can disappear.
