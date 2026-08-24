# Mobile conversation persistence

Tobkiri Mobile does not keep an authoritative local conversation database.
Conversation and message reads and mutations use the authenticated
`/api/mobile/v1/conversations` contract and are owned by the active
`rumi_conversation_store_pack` in the PC's captured Profile/ResolvedPlan.

This boundary intentionally replaces the retired mobile `ChatStore` and its
`rumi_chat.conversations.v1` / `rumi_chat.active_id.v1` keys. Those keys must
not be read, reset, or silently migrated by the current client. Reintroducing
them would create a second authority and could make a failed remote write look
saved locally.

## Save and load semantics

- A mutation is saved only after the authenticated host contract returns a
  success response. Transport errors, authorization errors, revision
  conflicts, and host persistence errors are failures; a client must retain a
  retryable/unsaved presentation or revert its optimistic state.
- Conversation selection is navigation state, not a separately persisted
  conversation transaction on the phone.
- Empty, unreadable, corrupt, and incompatible host stores are distinct: a
  missing store produces an empty revision-zero snapshot, while read,
  decoding, or schema/version failures propagate as errors. A failed load is
  never converted into an empty snapshot and therefore cannot be overwritten
  by a later mutation.
- Host writes are revision-checked and atomically published from an fsynced
  temporary file. A failed publish leaves the prior revision authoritative.
  Conversation and active-message identity are committed in the same owner
  snapshot; there is no second active-ID write.
- Legacy import is an explicit, source-hash-bound migration with a private
  backup and explicit rollback. It is never an implicit startup fallback.

The owner currently serializes a single revisioned profile snapshot. This is a
deliberate compatibility choice for atomic conversation/message links; the
mobile client never rewrites that snapshot itself. Per-conversation or journal
storage may replace the owner's physical format later, but only behind the
same v4 contracts and ProfileLock/ResolvedPlan authority.

## Recovery and export

On a conversation error, keep the PC store untouched and retry after fixing
the reported storage or authorization problem. Do not reset phone settings or
create a fresh local history as recovery. Explicit legacy migration backups
live under the profile-bound conversation-store Pack data directory and can be
rolled back only with the matching migration identifier.

Conversation export is an authenticated, audited host operation at
`POST /api/mobile/v1/conversations/{id}/export`. Diagnostics and support
records should include only error class/code, profile identity, revision, and
correlation data. Never include bearer tokens, approval material, raw secret
values, or unreviewed tool output.
