# Tobkiri Conversation Store Pack

Owns conversations and ordered messages in one revision-guarded atomic store.
It does not own turn execution, derived context, memory, or knowledge.

The canonical conversation and message Host actions capture an explicit Profile,
user-data root, Function/Operation binding and execution domain. They run only
after the normal Authority/Broker authorization; client `approved` flags do not
grant access. Message append/update/delete/replace require an exact positive
conversation revision. Append and replacement records also require stable IDs,
so retries cannot manufacture another message. Malformed replacement rows are
rejected rather than silently dropped.

The message action is registered as `runtime/message_host.py`. This source
registration does not activate a Profile binding or expose a chat-send endpoint.
Turn orchestration, AI streaming and cancellation remain separate integration
work; legacy ambient-root factories are not a canonical execution fallback.
