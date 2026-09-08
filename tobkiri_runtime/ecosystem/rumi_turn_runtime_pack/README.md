# Tobkiri Turn Runtime Pack

Owns bounded, revision-bound turn lifecycle state, idempotent begin requests,
steering, handoff references, cancellation, and ordered lifecycle events. It does
not persist conversations, messages, context, memory, or knowledge.

Current state is in-process, not durable across restart. This Pack is the owner
for future saved-turn lifecycle/status persistence; conversation storage remains
in the conversation owner Pack.

While a request is retained, repeated begin returns its current snapshot only
for the same Profile, conversation, original conversation revision and explicit
turn identity. Rebinding a request ID is a conflict. Begin and direct lifecycle
mutations require exact positive integer revisions, not booleans/coerced text.
This does not provide cross-restart idempotency or indefinite replay retention:
terminal pruning still removes old request mappings. Captured Host registration,
durable status/reconciliation and full-UI execution integration remain required.
