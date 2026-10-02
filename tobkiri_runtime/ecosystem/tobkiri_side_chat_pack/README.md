# Tobkiri Side Chat Pack

This optional Pack opens one side conversation for a parent conversation. The
child has its own messages and saved turn IDs. Parent messages are never copied
into its model input or history. The generic conversation owner persists an
immutable `tobkiri.conversation-context-link.v1` parent/slot under the same lock
and parent revision guard as creation. Concurrent requests cannot create two
children for a slot. Deleting a parent retains the child's history and lineage;
further sends become unavailable.

At send and every resumed effect, the runtime reads the parent through the
selected public conversation contract. The exact captured Profile, parent
revision and context digest form `tobkiri.conversation-context-binding.v1`.
Model, prompt and context references come from that owner read. A stale,
deleted, different-Profile or unresolved parent fails before the next effect.
Children cannot select a different model, workspace, agent, strategy, tool
scope or approval policy. Stored `approved` flags never grant authority; every
action uses the current Host policy and ordinary Broker authorization.

The current saved-turn baseline cannot resolve coding-workspace, agent, group
or shared-read-only contexts. Such parents report unavailable. No matching
public resolver is currently admitted, so this Pack does not claim to support
those contexts. Ordinary chat parents with an admitted model and optional
authored prompt use the canonical saved-turn path.

Public operations, version `1.0.0`:

- `tobkiri.resource.side-chat.v1` /
  `tobkiri_side_chat_pack.side-chat-resource`: `get` takes
  `parent_conversation_id`; `events` takes `conversation_id` and `turn_id`.
- `tobkiri.action.side-chat.v1` /
  `tobkiri_side_chat_pack.side-chat-manage`: `ensure` takes the parent ID and
  `expected_parent_revision`.
- `tobkiri.service.side-chat.turn.v1` /
  `tobkiri_side_chat_pack.side-chat-turn`: `send` takes child ID,
  `expected_child_revision`, `expected_parent_revision`, `turn_id`, and
  `content`. `stop` and `reconcile` take child ID and turn ID.

The Host injects `profile_id`; client identity, model and approval overrides are
rejected. Resource capture receives only read/event contracts. Management and
turn operations use credential-free clients with the exact declared public
edges. The Pack imports no other Pack implementation.

An explicit reasoning level in a linked saved request must equal the parent's
setting. If omitted, the Host derives it from the fresh bound parent context
without rewriting the saved input. The bridge rechecks parent context after
prompt/tool reads immediately before effect admission; saved appends also check
the parent binding in the conversation owner's atomic write transaction.

Sending delegates `tobkiri.action.turn.saved.v1` to preserve durable execution,
reconciliation and cancellation receipts. Cancellation and event reads first
prove that the turn belongs to the selected child, preventing a main turn or
another child's operation from being targeted. A stale send is not retried
automatically; reconciliation observes the existing receipt. The baseline
generation is buffered, and its events are operation events. This Pack does not
invent token streaming or infer completion from an accepted/cancel-requested
response.

`frontend/contributions/side-chat.json` uses the neutral declarative
`conversation_thread` renderer and shared composer/history surfaces. The UI
integration owner supplies that renderer. Missing Pack, provider, parent,
context or renderer stays unavailable.

Root integration inputs are in `integration-input.v1.json`. Promotion must add
the new Pack to the source catalog and semantic executable source registry,
register the exact Host factory, and admit the public dependency edges. The
PackVM guest closure separately needs
`"tobkiri_protocol/conversation_context.py":
"tobkiri_protocol/conversation_context.py"` in `SOURCE_FILES` in
`scripts/build_packvm_guest_bundle.py`, beside `saved_context.py`, and the same
path in `tests/test_packvm_guest_bundle.py`'s expected source set. The integration
owner regenerates bundles and all global artifacts; these source declarations
are not install, signing, release, real-model or Computer Use evidence.

PR #1244's separate-history/shared-composer behavior is adopted. Its
side-specific owner branches, legacy send-path override and generated migration
receipts are replaced or omitted. This branch depends on baseline PR #1496 and
the authenticated completion source in lifecycle PR #1497.
