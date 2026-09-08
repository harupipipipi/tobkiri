# Chat API

## Captured full-UI integration

The canonical map currently connects conversation CRUD to
`rumi_conversation_store_pack`, not the legacy handlers documented below.
The wire endpoint is `/api/contracts/defaultspack/` followed by the URL-encoded
method and target, for example `POST%20%2Fapi%2Fchat%2Fconversations`.
Normal panel authentication, CSRF, request identity, captured Profile grants
and Broker checks remain required.

- `GET /api/chat/conversations` returns `conversations`, `total` and
  `store_revision` from the captured owner snapshot.
- `POST /api/chat/conversations` requires `id` (canonical UUID) and
  `expected_revision` (that snapshot's nonnegative integer revision), alongside
  the UI's optional model/prompt/agent/tags/parent/kind/group/metadata fields.
  The result is the real created conversation, including its
  `conversation_revision` and the UI's `model` alias for `model_reference`.
- A repeated ID or stale revision is rejected without another write. The UI
  does not automatically retry conflicts. A transport failure is not proof that
  creation failed; refresh history before deciding whether another create is needed.
- Clients cannot provide `profile_id`, `approved`, owner operation, messages or
  a manufactured conversation revision. A new source Profile action edge needs
  normal activation review; existing live Profiles are not changed by this code.

- `GET /api/chat/conversation?conversation_id=...` returns the actual record,
  including its `conversation_revision`. This is a fixed route: a conversation
  ID is query data, never an unregistered path suffix.
- `PUT /api/chat/conversation` requires `conversation_id`,
  `expected_conversation_revision` and a nonempty `updates` object of mutable
  metadata fields. The UI sends the displayed revision, without a hidden
  refetch that could overwrite another edit. IDs, messages and manufactured
  revisions are not mutable metadata.
- `DELETE /api/chat/conversation` requires `conversation_id` and
  `expected_conversation_revision` in the JSON body. Only a confirmed owner
  deletion returns `deleted: true`; stale updates/deletes leave storage unchanged.

Message/stream/stop integration is still pending.
The isolated Conversation `complete` ABI now preserves an explicitly selected
`model` as a bounded `model_reference` in its digest-pinned AI request. Both guest
and Host reject malformed references. Profile identity, credentials, arbitrary
requirements and targets are still not forwarded from the outer UI payload;
the existing gateway resolves the reference through its captured contracts.
This bridge behavior alone does not connect the full UI's message/stream routes.
The following sections describe legacy APIs and are not evidence that those
operations are available through the captured full-UI map.

## Legacy API reference

defaults Pack のチャット機能の全 API リファレンスです。handler は `blocks/chat/` に、ドメインロジックは `domain/chat/store.py`（ChatStore）に実装されています。

ecosystem.json の chat コンポーネントは 18 個の handler を provides しています: `create_conversation`, `get_conversation`, `list_conversations`, `update_conversation`, `delete_conversation`, `export_conversation`, `send`, `stream`, `add_message`, `get_message`, `update_message`, `delete_message`, `branch`, `search`, `stop`, `regenerate`, `summarize_and_trim`, `auto_trim`。

## Provider-Agnostic Chat Pipeline

ChatStore remains the provider-agnostic source of truth. Stored Rumi messages are
converted to Rumi Chat IR v2 before provider planning. The legacy
`convert_to_standard()` API remains callable and still returns the historical
StandardMessage list used by existing provider adapters.

The runtime flow is:

```text
ChatStore messages
  -> Rumi Chat IR v2
  -> Provider Capability Registry
  -> Request Planner / degradation metadata
  -> legacy StandardMessage or Provider Compiler v2
  -> provider response parser
  -> assistant RumiMessage
```

`PreparedChatRun` now carries `chat_ir`, `ir_schema_version`,
`provider_capabilities`, and `provider_planning` alongside existing
`standard_messages`. Assistant metadata records the IR version, model routing,
chat references, planning warnings, dropped features, and provider trace info.

Rollback flags:

- `RUMI_DEFAULTSPACK_PROVIDER_LEGACY_MESSAGES=1`: force the legacy
  StandardMessage provider path.
- `RUMI_DEFAULTSPACK_PROVIDER_COMPILER_V2=1`: opt into Provider Compiler v2 for
  supported complete calls.

Provider trace artifacts are written under
`user_data/shared/chat/conversations/<conversation_id>/workspace/provider_traces/`.
They include redacted capability, planning, payload, and response summaries.

## External input conversations

External providers should not call chat internals with raw provider payloads.
Webhook and gateway intake should first produce an `ExternalEvent`, pass
`AudiencePolicy`, select an `InputProfile`, and call `submit_input`. The chat
layer then receives a normal user message with external metadata attached.

External conversations should use `conversation_kind: "external"` and stable
session keys such as `slack:{team_id}:{channel_id}:{thread_id}` or
`line:{source_type}:{source_id}`. Replies should be planned by
`ResponsePlanner` and delivered by a `ResponseAdapter`; chat handlers should not
hold raw provider tokens or construct provider API calls directly.

## 会話の作成

**handler**: `defaults.chat.create_conversation`（`blocks/chat/create_conversation.py`）

**HTTP**: `POST /api/chat/conversations`

**input_data**:

| フィールド | 型 | 必須 | 説明 |
|---|---|---|---|
| `model` | `string` | No | AI モデル名。デフォルト `"stub/default"` |
| `system_prompt_id` | `string` | No | システムプロンプト ID |
| `agent_id` | `string` | No | エージェント ID |
| `tags` | `string[]` | No | タグ配列。デフォルト `[]` |

**戻り値**（`ok(conv)`）:

```json
{
  "status": "ok",
  "data": {
    "id": "uuid",
    "title": "New Conversation",
    "created_at": 1700000000000,
    "updated_at": 1700000000000,
    "model": "stub/default",
    "system_prompt_id": null,
    "agent_id": null,
    "tags": [],
    "is_starred": false,
    "is_archived": false,
    "current_node_id": null,
    "messages": []
  }
}
```

## 会話の取得

**handler**: `defaults.chat.get_conversation`（`blocks/chat/get_conversation.py`）

**HTTP**: `GET /api/chat/conversations/{id}`

**input_data**:

| フィールド | 型 | 必須 | 説明 |
|---|---|---|---|
| `conversation_id` | `string` | Yes | 会話 ID（URL パスから自動注入） |

**戻り値**: `ok(conv)` — 会話オブジェクト全体（messages 含む）。見つからない場合は `error("Conversation not found", "NOT_FOUND")`。

## 会話の一覧

**handler**: `defaults.chat.list_conversations`（`blocks/chat/list_conversations.py`）

**HTTP**: `GET /api/chat/conversations`

**input_data**:

| フィールド | 型 | 必須 | 説明 |
|---|---|---|---|
| `limit` | `int` | No | 取得件数。デフォルト `50` |
| `offset` | `int` | No | オフセット。デフォルト `0` |
| `tag` | `string` | No | タグでフィルタ |
| `is_starred` | `bool` | No | スター状態でフィルタ |
| `is_archived` | `bool` | No | アーカイブ状態でフィルタ |

**戻り値**: `ok({"conversations": [...], "total": int})`。`updated_at` 降順でソートされます。

## 会話の更新

**handler**: `defaults.chat.update_conversation`（`blocks/chat/update_conversation.py`）

**HTTP**: `PUT /api/chat/conversations/{id}`

**input_data**:

| フィールド | 型 | 必須 | 説明 |
|---|---|---|---|
| `conversation_id` | `string` | Yes | 会話 ID（URL パスから自動注入） |
| `updates` | `dict` | Yes | 更新するフィールド。`id`, `created_at`, `messages` は変更不可 |

**戻り値**: `ok(conv)` — 更新後の会話オブジェクト。

## 会話の削除

**handler**: `defaults.chat.delete_conversation`（`blocks/chat/delete_conversation.py`）

**HTTP**: `DELETE /api/chat/conversations/{id}`

**input_data**:

| フィールド | 型 | 必須 | 説明 |
|---|---|---|---|
| `conversation_id` | `string` | Yes | 会話 ID（URL パスから自動注入） |

**戻り値**: `ok({"success": true})`。見つからない場合は `error("Conversation not found", "NOT_FOUND")`。

## メッセージの送信（AI 応答付き）

**handler**: `defaults.chat.send`（`blocks/chat/send.py`）

**HTTP**: `POST /api/chat/conversations/{id}/messages` または `POST /v1/chat/completions`

**input_data**:

| フィールド | 型 | 必須 | 説明 |
|---|---|---|---|
| `conversation_id` | `string` | Yes | 会話 ID |
| `message` | `dict` | Yes | メッセージオブジェクト |
| `message.role` | `string` | No | ロール。デフォルト `"user"` |
| `message.content` | `string` or `list` | Yes | メッセージ内容。文字列の場合は `[{"type": "text", "text": ...}]` に変換される |

**処理フロー**: ユーザーメッセージを `ChatStore.add_message()` で保存 → `get_message_chain()` で会話履歴を取得 → `convert_to_standard()` で標準形式に変換 → `call_handler("defaults.ai.complete", ...)` で AI 呼び出し → `build_assistant_message()` で assistant メッセージを構築 → `ChatStore.add_message()` で保存。

**戻り値**: `ok(assistant_msg)` — AI の応答メッセージオブジェクト。

```json
{
  "status": "ok",
  "data": {
    "id": "uuid",
    "conversation_id": "...",
    "parent_id": "user_msg_id",
    "children_ids": [],
    "sequence_number": 2,
    "role": "assistant",
    "content": [{"type": "text", "text": "AI response"}],
    "raw_text": "AI response",
    "created_at": 1700000000000,
    "finish_reason": "stop",
    "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
    "widget": null
  }
}
```

## メッセージの追加（AI 応答なし）

**handler**: `defaults.chat.add_message`（`blocks/chat/add_message.py`）

**input_data**:

| フィールド | 型 | 必須 | 説明 |
|---|---|---|---|
| `conversation_id` | `string` | Yes | 会話 ID |
| `message` | `dict` | Yes | メッセージオブジェクト（role, content） |

**戻り値**: `ok(msg)` — 追加されたメッセージオブジェクト。AI 呼び出しは行われません。

## メッセージの取得

**handler**: `defaults.chat.get_message`（`blocks/chat/get_message.py`）

**input_data**:

| フィールド | 型 | 必須 | 説明 |
|---|---|---|---|
| `conversation_id` | `string` | Yes | 会話 ID |
| `message_id` | `string` | Yes | メッセージ ID |

**戻り値**: `ok(msg)` — メッセージオブジェクト。

## メッセージの更新

**handler**: `defaults.chat.update_message`（`blocks/chat/update_message.py`）

**input_data**:

| フィールド | 型 | 必須 | 説明 |
|---|---|---|---|
| `conversation_id` | `string` | Yes | 会話 ID |
| `message_id` | `string` | Yes | メッセージ ID |
| `updates` | `dict` | Yes | 更新するフィールド。`id`, `conversation_id`, `created_at` は変更不可 |

**戻り値**: `ok(msg)` — 更新後のメッセージオブジェクト。

## メッセージの削除

**handler**: `defaults.chat.delete_message`（`blocks/chat/delete_message.py`）

**input_data**:

| フィールド | 型 | 必須 | 説明 |
|---|---|---|---|
| `conversation_id` | `string` | Yes | 会話 ID |
| `message_id` | `string` | Yes | メッセージ ID |

**戻り値**: `ok({"success": true})`。親メッセージの `children_ids` から自動的に削除されます。`current_node_id` が削除対象の場合は `parent_id` に更新されます。

## ストリーミング送信

**handler**: `defaults.chat.stream`（`blocks/chat/stream.py`）

**HTTP**: `POST /api/chat/conversations/{id}/stream`

**input_data**:

| フィールド | 型 | 必須 | 説明 |
|---|---|---|---|
| `conversation_id` | `string` | Yes | 会話 ID |
| `message` | `dict` | Yes | メッセージオブジェクト |

**処理**: ユーザーメッセージを保存し、`call_handler("defaults.ai.stream", ...)` でストリーミング AI 呼び出しを行います。`stream_id` が返され、これを使ってストリームの停止が可能です。

**戻り値**: `ok({"stream_id": "...", "conversation_id": "..."})`

## ストリーミング停止

**handler**: `defaults.chat.stop`（`blocks/chat/stop.py`）

**input_data**:

| フィールド | 型 | 必須 | 説明 |
|---|---|---|---|
| `conversation_id` | `string` | Yes | 会話 ID |
| `stream_id` | `string` | No | 停止するストリームの ID |

**戻り値**: `ok({"success": true})`

## AI 応答の再生成

**handler**: `defaults.chat.regenerate`（`blocks/chat/regenerate.py`）

**input_data**:

| フィールド | 型 | 必須 | 説明 |
|---|---|---|---|
| `conversation_id` | `string` | Yes | 会話 ID |
| `message_id` | `string` | Yes | 再生成対象のメッセージ ID |

**処理**: 指定メッセージを削除 → 親メッセージまでの会話チェーンを取得 → AI に再度送信 → 新しい assistant メッセージを保存。

**戻り値**: `ok(assistant_msg)` — 新しい AI 応答メッセージ。

## ブランチ（会話の分岐）

**handler**: `defaults.chat.branch`（`blocks/chat/branch.py`）

**HTTP**: 直接の HTTP ルートは未定義。`call_handler("defaults.chat.branch", ...)` 経由で呼び出します。

**input_data**:

| フィールド | 型 | 必須 | 説明 |
|---|---|---|---|
| `conversation_id` | `string` | Yes | 元の会話 ID |
| `message_id` | `string` | Yes | 分岐起点のメッセージ ID |

**処理**: `ChatStore.branch()` が指定メッセージまでのチェーンをコピーして新しい会話を作成します。新しい会話のタイトルには `" (branch)"` が付加されます。メッセージの `parent_id` / `children_ids` は新しい ID に再マッピングされます。

**戻り値**: `ok(new_conv)` — 分岐された新しい会話オブジェクト。

## 検索

**handler**: `defaults.chat.search`（`blocks/chat/search.py`）

**input_data**:

| フィールド | 型 | 必須 | 説明 |
|---|---|---|---|
| `query` | `string` | Yes | 検索クエリ |
| `conversation_id` | `string` | No | 特定の会話内に限定する場合 |

**処理**: `ChatStore.search()` が全メッセージの `raw_text` フィールドに対して大文字小文字を区別しない部分一致検索を行います。

**戻り値**: `ok({"results": [msg, msg, ...]})`

## エクスポート

**handler**: `defaults.chat.export_conversation`（`blocks/chat/export_conversation.py`）

**HTTP**: `POST /api/chat/conversations/{id}/export`

**input_data**:

| フィールド | 型 | 必須 | 説明 |
|---|---|---|---|
| `conversation_id` | `string` | Yes | 会話 ID |
| `format` | `string` | No | `"markdown"` または `"json"`。デフォルト `"markdown"` |

**戻り値**: `ok({"content": "..."})`。`domain/chat/exporter.py` の `export_markdown()` または `export_json()` が呼ばれます。

## 会話履歴の AI 要約（summarize_and_trim）

**handler**: `defaults.chat.summarize_and_trim`（`blocks/chat/summarize_and_trim.py`）

**HTTP**: `POST /api/chat/conversations/{id}/summarize`

**input_data**:

| フィールド | 型 | 必須 | 説明 |
|---|---|---|---|
| `conversation_id` | `string` | Yes | 会話 ID |
| `start_message_id` | `string` | Yes | 要約範囲の開始メッセージ ID |
| `end_message_id` | `string` | Yes | 要約範囲の終了メッセージ ID |
| `model` | `string` | No | 要約に使用する AI モデル。`"default"` の場合は会話のモデルを使用 |
| `instruction` | `string` | No | 追加の要約指示 |

**処理**: 指定範囲のメッセージを取得 → `convert_to_standard()` で標準形式に変換 → 要約用プロンプトを構築 → AI に要約させる → 範囲内メッセージを一括削除（`delete_messages_bulk`） → 要約メッセージを挿入（`insert_message_at`）。要約メッセージの `metadata` には `is_summary: true` と `original_message_ids` が含まれます。

**戻り値**:

```json
{
  "status": "ok",
  "data": {
    "conversation": { "...updated conversation..." },
    "summary_message": { "...summary msg..." },
    "deleted_message_ids": ["id1", "id2", "..."]
  }
}
```

## 会話履歴の AI 自動トリム提案（auto_trim）

**handler**: `defaults.chat.auto_trim`（`blocks/chat/auto_trim.py`）

**HTTP**: `POST /api/chat/conversations/{id}/auto-trim`

**input_data**:

| フィールド | 型 | 必須 | 説明 |
|---|---|---|---|
| `conversation_id` | `string` | Yes | 会話 ID |
| `model` | `string` | No | 分析に使用する AI モデル。`"default"` の場合は会話のモデルを使用 |
| `max_context_tokens` | `int` | No | トリム後の目標トークン数 |

**処理**: 会話の全メッセージを取得 → 各メッセージの content からテキストを抽出 → AI に分析プロンプトを送信 → AI が要約可能なセグメントを JSON 配列で返す → メッセージ ID の存在チェックでバリデーション。

**戻り値**:

```json
{
  "status": "ok",
  "data": {
    "trim_plan": {
      "segments": [
        {
          "start_id": "msg_id_1",
          "end_id": "msg_id_5",
          "reason": "Intermediate debug outputs",
          "summary_preview": "Debugging session that resolved the issue"
        }
      ]
    },
    "conversation_id": "...",
    "total_messages": 20
  }
}
```

返された `segments` の各 `start_id` / `end_id` を `summarize_and_trim` に渡すことで実際のトリムを実行できます。

## 全 API エンドポイント一覧

| メソッド | パス | handler ファイル |
|---|---|---|
| `POST` | `/v1/chat/completions` | `blocks/chat/send.py` |
| `POST` | `/api/chat/conversations` | `blocks/chat/create_conversation.py` |
| `GET` | `/api/chat/conversations` | `blocks/chat/list_conversations.py` |
| `GET` | `/api/chat/conversations/{id}` | `blocks/chat/get_conversation.py` |
| `PUT` | `/api/chat/conversations/{id}` | `blocks/chat/update_conversation.py` |
| `DELETE` | `/api/chat/conversations/{id}` | `blocks/chat/delete_conversation.py` |
| `POST` | `/api/chat/conversations/{id}/messages` | `blocks/chat/send.py` |
| `POST` | `/api/chat/conversations/{id}/stream` | `blocks/chat/stream.py` |
| `POST` | `/api/chat/conversations/{id}/export` | `blocks/chat/export_conversation.py` |
| `POST` | `/api/chat/conversations/{id}/summarize` | `blocks/chat/summarize_and_trim.py` |
| `POST` | `/api/chat/conversations/{id}/auto-trim` | `blocks/chat/auto_trim.py` |
