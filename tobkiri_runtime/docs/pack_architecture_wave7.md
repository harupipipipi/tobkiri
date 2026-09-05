# Pack Architecture Wave 7

Wave 7 assigns one owner to conversation, message, turn, memory, and knowledge.
Context is explicitly derived and has no authoritative persisted state.

## Owner boundaries

- `rumi_conversation_store_pack` owns conversations and their ordered messages
  in one profile-bound atomic state. Message append/delete/replacement also
  updates the current node in the same transaction.
- `rumi_turn_runtime_pack` owns bounded process-lifetime turn lifecycle,
  steering guidance, handoffs, and ordered events. It owns no messages.
- `rumi_context_runtime_pack` materializes a digest-bound projection from exact
  conversation, memory, and knowledge revisions. It persists nothing.
- `rumi_memory_store_pack` owns durable memory, memo, project-context, dream,
  daily, and wiki records in one state. SQLite, Markdown, and vector names are
  deprecated projections, not additional stores.
- `rumi_knowledge_store_pack` owns source knowledge. Embeddings and indexes are
  disposable projections and the default search needs no network or key.

Defaultspack retains finite compatibility facades. They resolve exactly one
selected global provider and fail closed; none falls back to an old store.

## Data ownership matrix

| Resource | Authoritative pack | Schema version | Storage | Backup | Migration | Rollback | Retention | Export/import |
|---|---|---:|---|---|---|---|---|---|
| conversations | `rumi_conversation_store_pack` | 1.0.0 | profile atomic JSON | owner-only migration snapshot | source-hash import | marker-bound exact restore | explicit deletion/profile removal | contract JSON |
| messages | `rumi_conversation_store_pack` | 1.0.0 | same conversation transaction | same conversation backup | same conversation import | same marker restore | conversation lifetime | contract JSON |
| turns | `rumi_turn_runtime_pack` | 1.0.0 | bounded in-process owner | none | none | restart/remove pack | active plus bounded terminal set | event projection |
| context | none; derived by `rumi_context_runtime_pack` | 1.0.0 | none | none | contract cutover | remove projection | request lifetime | digest-bound result |
| memory | `rumi_memory_store_pack` | 1.0.0 | profile atomic JSON | owner-only migration snapshot | source-hash import | marker-bound exact restore | expiry/deletion/profile removal | contract JSON |
| knowledge | `rumi_knowledge_store_pack` | 1.0.0 | profile atomic source JSON | owner-only migration snapshot | source-hash import | marker-bound exact restore | explicit deletion/profile removal | contract JSON |
| integration conversation mapping | defaultspack pending Wave 9 connector split | existing | connector mapping only; no messages | existing | Wave 9 | Wave 9 | connector policy | connector export |

## Compatibility removal

The following defaultspack names are sunset facades scheduled for removal in
Wave 10: `ChatStore`, `ConversationSteerStore`, `MemoryStore`,
`MemorySQLiteStore`, `MarkdownMemoryStore`, `MemoStore`, and `KnowledgeStore`.
They do not own data. Workspace attachment artifacts remain a separate artifact
surface and are not conversation/message state.

## Static implementation audit

- No conversation or steer JSON write remains in their legacy facade modules.
- Memory2 opens no SQLite connection and writes no Markdown/wiki owner file.
- Knowledge facade does not embed, persist, or search a legacy store.
- Context source resolution uses declared contracts only.
- Profile normalization selects all five Wave 7 runtime packs together.

Focused tests are defined in `tests/test_pack_architecture_wave7.py` but were
not executed by the implementation agent.

テスト、lint、build、起動確認、実環境確認は実装担当のCodexでは実行していません。
マージ前に独立した実環境QAが必要です。
