# Exact owner/Workflow binding investigation

No canonical Pack asset, metadata or receipt pin changes are made by this report.
Only public docs, JSON schemas and Contract/Operation declarations were inspected.

## Read the existing durable owner

Public Contract `tobkiri.resource.conversation.v1`, revision
`sha256:254d3ee4bdbe11bf21f658bbb3b89a1d6510ef7dbf77fb9108dcc4b426496156`,
Operation `rumi_conversation_store_pack.conversation-resource`, exact Function ID
`rumi_conversation_store_pack.conversation-store.resource` exposes owner reads.
The documented input is `operation: get`, captured `profile_id` and exact
`conversation_id`. It requires the existing `conversation.read` / Host broker
ceiling. A declared request does not establish those identities or authorize it.
Its public input/output schemas are objects without a precise response wrapper.
That wrapper must be documented before authoring a step-output mapping into a
subsequent Function; it is not guessed here.

`owner-read.workflow.intent.v1.json` is an outside-Pack, schema-valid source draft
for that exact read. It uses public `${inputs.field}` parameters and creates no
runtime principal, Grant, invocation, trusted event or store. It has not been
selected or executed. No second timestamp store is proposed.

## Do not confirm completion from this extension

`tobkiri.action.conversation.completion.v1` / Operation
`rumi_conversation_store_pack.conversation-completion` accepts only Profile,
`operation: confirm`, turn ID (and the documented session field). Public adoption
notes restrict caller identities to saved-turn and reconciliation Functions.
Independent reducer events cannot replace durable confirmation, and an external
Workflow must not claim authority to invoke this restricted completion action.

## Invocation is separate from preview

Public Workflow v4 map supplies these payload names:

- `definition.create`: `definition_id`, `document`.
- `definition.publish`: `definition_id`, `if_match`.
- `definition.compile-preview`: `document`.
- `run.create`: `definition_id`, `inputs`, `occurrence_id`, `run_id`.
- `run.advance`: `run_id`.
- `run.step.execute`: `run_id`, `step_id`.

The exact Workflow Contract revision is
`sha256:6edd3594528abb71a078924d0479388eff2f0c22da481f4887bd027b72736a31`.
The mapped provider Function is `tobkiri.workflow.provider`. The public provider
document requires captured Contract catalog, input validator, broker invocation
and Authority lifecycle providers. Publication, Run creation and execution must
use normal approval-aware calls, not direct callbacks or a fabricated reservation.
The operation schemas are bounded generic objects; they do not fully document
response receipts/revisions. No live command or Run is invoked by this author.

## Remaining composition interfaces

1. A documented trusted trigger after the accepted next-user owner transaction,
   with captured Profile/conversation identity. Caller event labels are insufficient.
2. Exact owner-resource result wrapper and public Workflow prior-step-output
   expression syntax. The published source format documents `${inputs.field}`;
   no result path is invented here.
3. A selected extension hook into native saved-turn/request input construction.
   `saved_conversation_input_v1.schema.json` rejects arbitrary temporal context,
   system-message, timestamp and approval fields. Its `task_context` is a closed
   saved task record, and `context_binding` pins a parent revision/context digest;
   neither is a generic sink for reducer output.
4. Evidence from the real selected Workflow/Host route that the owner read reaches
   the native bridge under unchanged user text and existing tool policy. Owner SDK
   projections alone do not authenticate arbitrary dictionaries.

The native saved bridge already uses the owner's durable timing and a separate
system message. A Workflow that merely reads the owner, or invokes the saved turn
without consuming an extension result, must not be described as integrating this
independent reducer into that internal context path. A new typed hook, if needed,
should reuse the existing owner baseline and provenance rather than caller snapshots.

## Public follow-up now authored

`docs/selected_conversation_workflow.md` now publishes direct successful sealed
`${steps.<id>.output.path}` dependencies, the owner envelope path
`${steps.owner.output.value.conversation}`, `run.selected`, and a closed typed
`contexts/*.conversation-context.v1.json` timing sink. Those documentation gaps
are resolved; the earlier external input rejection remains intentional.

The independent candidate now authors owner-read -> stateless timing.project.owner
and that output binding. Its public Pack compiler and SDK parity tests pass. Current
named Profile compilation fails because the published template has no resolvable
`run.selected` executable target. That concrete publication gap is preserved rather
than bypassed. Prior candidate source success must not be reused as current approval
or execution evidence. Native bridge re-read/typed validation and real attempt
execution remain Root verification, not author-generated trust.

## Publication and edge identity resolved

The public template now publishes the Workflow executable `run.selected` target.
`docs/named_profile_authoring.md` clarifies that requested-edge `target_provider_id`
is the selected executable Function ID, not a Contract provider declaration label.
Source now supplies the producer's actual `.reduce` or explicitly renamed Function
ID; the original ambiguous failure is preserved separately. No alias resolution or
principal guess is used. All 22 tests and fresh named Profile compilation pass.
These resolve the earlier source-publication/documentation errors, not prove native
execution. The current receipt records exact new source pins; the earlier pending
receipt is labeled historical/superseded.
