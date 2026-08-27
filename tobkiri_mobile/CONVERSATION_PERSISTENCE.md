# Mobile conversation persistence

Tobkiri Mobile keeps on-device conversations for local-first chat. PC spaces
remain owned by the authenticated PC conversation service and are not copied
into local storage unless the user explicitly chooses to continue locally.

## Durable local snapshot

Local conversations and the active conversation ID are published together in
one versioned `tobkiri_chat.snapshot.v2` value. A successful mutation returns a
durable snapshot revision only after the platform write is read back and
verified. The previous verified snapshot is retained as a backup before the
next revision is published.

The legacy `rumi_chat.conversations.v1` and `rumi_chat.active_id.v1` values are
read only for migration. A fully valid legacy history is converted to the v2
snapshot before mutations continue. A partially valid legacy history is shown
as a recovery candidate and is never published until the user accepts it.

## Failure and recovery semantics

- Missing, unreadable, corrupt, incompatible, and partially recoverable data
  are distinct load outcomes. Read or decode failure never becomes an empty
  history and never authorizes an overwrite.
- Create, select, rename, pin, delete, message append, streamed delta, and
  explicit persistence operations report storage failure. When publication
  fails, in-memory state is restored from the last verified snapshot, so the
  UI cannot present the failed change as saved.
- A valid backup may be inspected in read-only recovery mode. The user must
  explicitly choose **復元** before a new revision can replace the damaged
  primary snapshot.
- An ambiguous platform write is treated as successful only when read-back
  exactly matches the proposed snapshot. Otherwise it is a failure and the
  in-memory mutation is rolled back.
- Recovery and save errors expose stable error codes and operation names, not
  raw conversation content, credentials, approval material, or platform error
  strings.

The current physical format is a bounded compatibility step: one snapshot
keeps conversation/message links and active ID atomic at the preference
boundary. A future per-conversation store or journal may reduce whole-history
writes, but it must preserve the same revision, rollback, backup, and
fail-closed recovery contract.
