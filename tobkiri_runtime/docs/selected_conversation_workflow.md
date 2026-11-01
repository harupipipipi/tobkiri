# Selected conversation context Workflows

Workflow inputs support whole-value `${inputs.path}` and
`${steps.<step-id>.output.path}` references. The output source must be a direct
`depends_on` dependency. It must have exactly one successful, non-skipped sealed
attempt in the same run. Waiting, failed, cancelled, ambiguous, skipped or other
run output cannot be used. Paths traverse objects only; there is no expression
execution, interpolation or client-supplied output lookup. Materialized values
are validated against the actual target input schema before reserve/commit and
normal Broker invocation. Captured Broker output is the complete outcome:
`{"status":"ok","value": ...}`. For the conversation resource, the owner snapshot
is `${steps.owner.output.value.conversation}`.

`run.selected` on `tobkiri.workflow.v4` accepts exactly `definition_id`, `inputs`
and a bounded `occurrence_id`. It resolves only currently selected immutable
Workflow source, binds exact Functions through the captured operation palette,
and stores a content-addressed compiled definition. It creates/drives a sealed
run through the existing attempt authority adapter/Broker. It does not approve a
pending step. Returned `run` and `attempts` describe actual engine state; a
compile preview is not execution. Normal selected edges, Host capture, grants,
interactive approval and VM execution remain required.

A selected Pack content projection may declare
`contexts/<id>.conversation-context.v1.json` with exactly:

```json
{
  "context_binding_api_version": "io.tobkiri.conversation-context-binding.v1",
  "kind": "timing.task_gap",
  "workflow_id": "my.conversation.context",
  "output_step_id": "context"
}
```

At most one timing binding is permitted. The saved-turn Host bridge supplies
captured `profile_id` and `conversation_id` as Workflow inputs, not arbitrary
request events, timestamps or state. Author a first step requesting
`tobkiri.resource.conversation.v1` /
`rumi_conversation_store_pack.conversation-resource` with `operation: get` and
those captured identities. A dependent Pack step may receive that owner's
snapshot and emit the closed typed result below. It must not confirm completion
or maintain another timestamp store. Caller-provided snapshots stay untrusted
outside this captured execution path.

The timing output has exactly `internal_context_api_version`
(`io.tobkiri.saved-internal-context.v1`), `profile_id`, `conversation_id`,
`conversation_revision`, `active_user_message_id`, and `context`. `context` is
null or the existing public `active_task_gap_context(owner_snapshot)` projection:
`version`, `completion_message_id`, `previous_task_completed_at`,
`current_user_message_at`, `elapsed_seconds`. No roles, arbitrary messages,
approval flags, paths or mutable completion baselines are accepted.

The bridge re-reads the captured owner after execution and checks the exact
revision/node/lifecycle and typed output before replacing its default timing
projection. It renders the existing `task_gap_prompt` once as a separate system
message, preserving user text. Unselected, mutated, duplicate, stale, mismatched
namespace/receipt/role or waiting/denied Workflow output fails closed. Empty
selection preserves existing behavior. These Source interfaces do not establish
native Launcher/Host/PackVM acceptance; tests must label isolated execution.
