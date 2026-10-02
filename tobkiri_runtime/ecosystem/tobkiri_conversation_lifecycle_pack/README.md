# Tobkiri Conversation Lifecycle Pack

This optional Pack archives conversations whose explicitly selected mode is
`archive_after_completion`. It reads the canonical conversation owner's
`tobkiri.conversation-lifecycle.v1` source and archives at or after
`completed_at_ms + 3_600_000` using an exact conversation revision. History is
retained. Manual conversations keep their existing behavior.

Successful final assistant completion is recorded in the owner transaction.
Running, streaming, user-answer wait, approval wait, cancellation and failure do
not create an archive deadline. A new user message cancels the old deadline;
the next successful completion creates a new one. Completion timestamps survive
later content/title edits. Old imported history without an owner completion
source is deliberately unknown and is never automatically migrated or archived.

Selecting the mode first creates or reuses a durable 60-second scheduler wakeup
through the public schedule contracts. At every wakeup and after restart the
Pack reads the owner again. A resumed turn between the read and archive write
invalidates CAS; a failed dependency or unconfirmed write is reported as
unavailable. Disabling the Pack removes its selected provider binding, so the
scheduler cannot invoke an absent adapter. Re-enabling recovers persisted dates.
The scheduler clock must be running for wakeups to execute; a saved setting
alone is not execution evidence. Scans have minute-level delivery latency and
never archive before the exact 3,600-second boundary.

Public operations, all version `1.0.0`:

- `tobkiri.resource.conversation.lifecycle.v1` /
  `tobkiri_conversation_lifecycle_pack.lifecycle-status`: `operation: get`,
  `profile_id`, `conversation_id`.
- `tobkiri.action.conversation.lifecycle.v1` /
  `tobkiri_conversation_lifecycle_pack.lifecycle-manage`: `operation: configure`,
  `profile_id`, `conversation_id`, `mode: manual | archive_after_completion`.
- `tobkiri.action.job.adapter.v1` /
  `tobkiri_conversation_lifecycle_pack.archive-job-adapter`: `describe` exposes
  action `conversation-lifecycle.archive-due`; `dispatch`, `cancel`, and `status`
  use the scheduler's exact job envelope. Unknown past outcomes stay unknown.

The Pack imports no other Pack implementation. Host capture supplies an exact
Profile and a restricted public contract client. All scheduling and archive
writes retain ordinary Authority/Broker authorization, capability checks and
audit. Client approval flags, arbitrary paths, clocks and URLs are rejected.

Defaults also consumes the public completion source when preparing the next
user turn. A gap of at least 3,600 seconds becomes a system-role runtime message
containing the completion timestamp, message receipt timestamp and UTC elapsed
seconds. User-authored content stays unchanged. The context grants no approval
and does not force a web/tool call.

PR #1410's internal-context behavior is adopted; its `updated_at` inference,
streaming-only exclusions, and generated migration evidence are omitted in
favor of a canonical immutable completion source and explicit task states.

Metadata declarations are source review inputs. They do not claim installed,
enabled, signed, released, real-model, or Computer Use validation. Global
catalog/Profile admission and UI composition belong to the integration owner.
