# Tobkiri Turn Runtime Pack

Owns bounded, revision-bound turn lifecycle state, idempotent begin requests,
steering, handoff references, cancellation, and ordered lifecycle events. It does
not persist conversations, messages, context, memory, or knowledge.

Canonical lifecycle, resource and event functions use `runtime/host.py`, capturing
the exact Profile, owner root and provider domain. They share `runtime/durable.py`
SQLite state. Capture and absent reads create no files. Writes require exact
revisions and serialize across processes. Events remain ordered persisted
snapshots returned by get/list, not a live streaming subscription.

While a request is retained, repeated begin returns its current snapshot only
for the same Profile, conversation, original conversation revision and explicit
turn identity. Rebinding a request ID is a conflict. Begin and direct lifecycle
mutations require exact positive integer revisions, not booleans/coerced text.
Durable terminal request identities are not pruned; capacity exhaustion rejects
new turns rather than admitting a replay. The legacy in-process factory still
prunes terminal mappings and must not be used for saved-send recovery.

Durability is not execution recovery: a restored running state does not authorize
another AI invocation or uncertain write. Reconciliation, provider cancellation,
full-UI execution integration and native activation remain required. Registration
does not add a live Profile edge or bypass Authority/Broker checks.
