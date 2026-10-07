# Public boundary for Issue #1409 and named selection

The independent temporal Pack and named Profile source are authored through public
producers. Exact Pack-origin projection selection and Workflow source binding are
separate from runtime execution and completion/context integration. No source lock,
activation, approval, active principal, or engine is fabricated by this author.

## Existing public interfaces

- `docs/python_pack_authoring.md` supplies the executable Python Normal Pack ABI,
  public producer and actual offline artifact compiler.
- `docs/named_profile_authoring.md`, Profile intent/projection schemas and
  `profile_workflow_intent_v1.schema.json` supply named source selection, exact
  Pack-origin content pins and exact Workflow Function/Contract/revision/Operation
  source bindings. The selected compiler derives a principal only from exactly one
  matching actual captured operation-palette entry.
- Prompt resources use `prompts/*.system.md`: plain Markdown, filename ID, no
  frontmatter. Legacy `flows/*.flow.yaml` aliases fail `V4_OPERATION_UNAVAILABLE`.
  There is no `functions/*.function.yaml` registration format.
- `docs/public_conversation_lifecycle.md` and public
  `tobkiri_protocol.conversation_lifecycle` already document durable owner-confirmed
  completion and next-user receipt timing, UTC gap projections, and separate system
  model-message delivery in the native saved-turn bridge. These are existing
  repository interfaces, not missing APIs or independent-extension achievements.

## Remaining independent composition

Obtain owner-bound conversation data through the selected, captured
`tobkiri.resource.conversation.v1` Contract, Operation
`rumi_conversation_store_pack.conversation-resource`, using `operation: get` with the
captured `profile_id` and exact `conversation_id`. The owner binds Profile/store
identity, confirms final completion against durable turn/receipt evidence, and
stores next-user timing in its transaction. Tool events, waiting, cancellation,
failed work and stale parallel terminals cannot establish successful completion.
The SDK projections do not authenticate arbitrary mappings supplied by callers.

This independent reducer still receives caller-supplied namespace/event/snapshot
values. Its selected Workflow source and successful compiler checks do not turn
those values into authenticated owner evidence. The remaining work is connecting
the independent selected Workflow to the existing trusted owner and native
saved-turn/request context bridge, preserving unchanged user text and existing
tool/approval policy. Reuse the owner's durable completion baseline; do not replace
that path with a second timestamp store.

The author has verified public schemas, deterministic Pack builds, actual source
compiler routes, selected/unselected/removal/renamed Profile compilation, and
expected retained-projection denial. Root owns captured-palette binding and
selected runtime-consumer verification. Neither author tests nor compile-preview
alone demonstrate trusted owner settlement, native model delivery, live Launcher,
Host/PackVM execution, activation, or completion of this extension's Issue #1409
integration. No private runtime implementation or Root adapter is used here.
