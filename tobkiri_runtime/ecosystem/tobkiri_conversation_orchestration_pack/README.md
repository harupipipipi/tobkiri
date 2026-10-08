# Tobkiri Conversation Orchestration

This normal sandbox Pack owns the saved-conversation continuation implementation.
Tobkiri Harness (`defaultspack`) owns presentation; the Conversation Store owns
messages and receipts; the Turn Runtime owns durable turn claims and cancellation.

## Stable public contract

- Contract: `conversation.saved-turn.v1` version `1.0.0`
- Operation: `saved_complete`
- Provider: `tobkiri_conversation_orchestration_pack.saved`
- Runtime artifact: `runtime/saved_conversation.py`

The implementation performs no direct storage or network I/O. It emits bounded
intents; the authenticated guest supervisor and captured Host Broker resolve the
exact selected contract edges. A request or intent never grants authority.

The Defaults Profile explicitly selects this Pack. The Turn Runtime is its only
saved-turn ingress. There is no direct Shell-to-saved-execution edge. Message
receipts accept only this captured saved provider, not the retired
`defaultspack.conversation.saved` principal. Existing installed Profiles require
normal explicit regeneration/reconfirmation; old grants are not aliased.

## Composable message-assembly node

- Contract: `tobkiri.service.conversation.messages.build.v1`, version `1.0.0`
- Operation: `messages_build`
- Provider: `tobkiri_conversation_orchestration_pack.messages`
- Input: `conversation` and `system_prompt` (an explicitly resolved prompt or `null`)
- Output: `messages`

The Defaults Profile admits one Workflow-to-node edge. This distinct provider
has no outgoing edges and cannot read storage, call a model, or save a message.
It runs as an ordinary pure PackVM operation, with 512 KiB canonical input and
output bounds. Its declared ports are data ports, not approval or credential
configuration.

Supply a conversation read by the preceding owner node. Assembly selects the
current message's ancestor branch, preserves roles and complete tool-call/result
pairs, and prepends the resolved system prompt. The newest two inline images
are retained; older images receive explicit omission markers. External image
URLs and unresolved content are rejected. Prompt digest validation checks data
consistency only; it does not authenticate the caller or grant authority.

Structured text blocks, image blocks and tool messages remain structured. The
existing `tobkiri.service.ai.messages.generate.v1` node accepts only plain-text
system/user/assistant messages, so richer output is not automatically compatible
with that node. Do not silently drop or stringify unsupported content to connect
them. Additional model nodes can consume the same message data under their own
documented input contracts.

This node is independently registered and tested. The ordinary saved-chat entry
path still uses `saved_complete`; registering the node does not switch existing
conversations to a graph. The future save boundary must additionally verify the
admitted run, published revision, owner/session identity and live input lineage.

## Authoring

Canonical manifest source lives in `schemas/pack_v4_catalog.v1.json`.
The executable source declaration lives in
`tests/fixtures/legacy_executable_sources.v1.json` (the filename is historical).
Use the official source-registry, Pack-artifact, executable-catalog and Defaults
bundle generators. Do not edit generated digests or authority receipts by hand.

## Scope

This extraction preserves the existing saved-turn behavior and continuation ABI.
It does **not** yet convert the saved-turn stages into a Workflow v4 graph.
That migration must retain revision/branch/prompt validation, exact receipt
ownership, cancellation, and no replay after an uncertain external effect.
