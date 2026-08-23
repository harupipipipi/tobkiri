# API Reference

defaults Pack の HTTP transport (`transport/http.py`) が公開する全エンドポイント。

すべてのレスポンスは JSON 形式で、成功時は `{"status": "ok", "data": ...}`、エラー時は `{"status": "error", "error": {"code": "...", "message": "..."}}` を返す。

CORS ヘッダーはすべてのレスポンスに付与される: `Access-Control-Allow-Origin: *`, `Access-Control-Allow-Methods: GET, POST, PUT, DELETE, OPTIONS`, `Access-Control-Allow-Headers: Content-Type, Authorization`。

---

## Chat — 会話管理

### POST /v1/chat/completions

OpenAI 互換エンドポイント。メッセージを送信して AI レスポンスを取得する。内部で `blocks.chat.send` を呼び出す。

**Request Body:**

| パラメータ | 必須 | 型 | 説明 |
|---|---|---|---|
| `conversation_id` | 必須 | `string` | 会話 ID |
| `message` | 必須 | `object` | `{"role": "user", "content": "..."}` 形式のメッセージ |
| `message.role` | 任意 | `string` | ロール。デフォルト `"user"` |
| `message.content` | 必須 | `string \| array` | テキスト文字列または content block 配列 |

**Response (`data`):**

| フィールド | 型 | 説明 |
|---|---|---|
| `id` | `string` | アシスタントメッセージ ID |
| `conversation_id` | `string` | 会話 ID |
| `role` | `string` | `"assistant"` |
| `content` | `array` | `[{"type": "text", "text": "..."}]` |
| `parent_id` | `string` | 親メッセージ ID |
| `sequence_number` | `int` | シーケンス番号 |
| `created_at` | `int` | 作成タイムスタンプ（ミリ秒） |
| `finish_reason` | `string \| null` | `"stop"` 等 |
| `usage` | `object \| null` | `{"prompt_tokens": int, "completion_tokens": int, "total_tokens": int}` |

**エラーケース:**

| コード | 説明 |
|---|---|
| `INVALID_INPUT` | `conversation_id` または `message` が未指定 |
| `NOT_FOUND` | 指定の会話が存在しない |
| `INTERNAL_ERROR` | メッセージ追加に失敗 |

---

### POST /api/chat/conversations

新しい会話を作成する。内部で `blocks.chat.create_conversation` を呼び出す。

**Request Body:**

| パラメータ | 必須 | 型 | 説明 |
|---|---|---|---|
| `model` | 任意 | `string` | 使用モデル。デフォルト `"stub/default"` |
| `system_prompt_id` | 任意 | `string` | システムプロンプト ID |
| `agent_id` | 任意 | `string` | エージェント ID |
| `tags` | 任意 | `array[string]` | タグ |

**Response (`data`):**

| フィールド | 型 | 説明 |
|---|---|---|
| `id` | `string` | 会話 ID (UUID) |
| `title` | `string` | `"New Conversation"` |
| `created_at` | `int` | 作成タイムスタンプ |
| `updated_at` | `int` | 更新タイムスタンプ |
| `model` | `string` | モデル文字列 |
| `system_prompt_id` | `string \| null` | システムプロンプト ID |
| `agent_id` | `string \| null` | エージェント ID |
| `tags` | `array[string]` | タグ |
| `is_starred` | `bool` | スター状態 |
| `is_archived` | `bool` | アーカイブ状態 |
| `current_node_id` | `string \| null` | 現在のノード ID |
| `messages` | `array` | メッセージ配列（初期は空） |

---

### GET /api/chat/conversations

会話一覧を取得する。内部で `blocks.chat.list_conversations` を呼び出す。

**Request Body:** なし（GET のためクエリパラメータは Body 不要）

**Response (`data`):**

| フィールド | 型 | 説明 |
|---|---|---|
| `conversations` | `array[object]` | 会話オブジェクトの配列 |
| `total` | `int` | 総件数 |

**エラーケース:** なし（空配列を返す）

---

### GET /api/chat/conversations/{id}

指定 ID の会話を取得する。パスパラメータ `{id}` が `conversation_id` として注入される。

**パスパラメータ:**

| パラメータ | 必須 | 型 | 説明 |
|---|---|---|---|
| `id` | 必須 | `string` | 会話 ID |

**Response (`data`):** 会話オブジェクト（POST /api/chat/conversations の Response と同形式）

**エラーケース:**

| コード | 説明 |
|---|---|
| `NOT_FOUND` | 指定の会話が存在しない |

---

### PUT /api/chat/conversations/{id}

会話のメタデータを更新する。

**パスパラメータ:**

| パラメータ | 必須 | 型 | 説明 |
|---|---|---|---|
| `id` | 必須 | `string` | 会話 ID |

**Request Body:**

| パラメータ | 必須 | 型 | 説明 |
|---|---|---|---|
| `title` | 任意 | `string` | 新しいタイトル |
| `tags` | 任意 | `array[string]` | 新しいタグ |
| `is_starred` | 任意 | `bool` | スター状態 |
| `is_archived` | 任意 | `bool` | アーカイブ状態 |
| `model` | 任意 | `string` | モデル変更 |

**Response (`data`):** 更新後の会話オブジェクト

**エラーケース:**

| コード | 説明 |
|---|---|
| `NOT_FOUND` | 指定の会話が存在しない |

---

### DELETE /api/chat/conversations/{id}

会話を削除する。内部で `blocks.chat.delete_conversation` を呼び出す。

**パスパラメータ:**

| パラメータ | 必須 | 型 | 説明 |
|---|---|---|---|
| `id` | 必須 | `string` | 会話 ID |

**Response (`data`):** `{"success": true}`

**エラーケース:**

| コード | 説明 |
|---|---|
| `NOT_FOUND` | 指定の会話が存在しない |

---

### POST /api/chat/conversations/{id}/messages

会話にメッセージを送信して AI レスポンスを取得する。`/v1/chat/completions` と同じ block（`blocks.chat.send`）を呼び出すが、`conversation_id` がパスから注入される。

**パスパラメータ:**

| パラメータ | 必須 | 型 | 説明 |
|---|---|---|---|
| `id` | 必須 | `string` | 会話 ID |

**Request Body:**

| パラメータ | 必須 | 型 | 説明 |
|---|---|---|---|
| `message` | 必須 | `object` | `{"role": "user", "content": "..."}` |

**Response (`data`):** アシスタントメッセージオブジェクト（POST /v1/chat/completions と同形式）

**エラーケース:** POST /v1/chat/completions と同じ

---

### POST /api/chat/conversations/{id}/stream

ストリーミングレスポンスを開始する。内部で `blocks.chat.stream` を呼び出す。

**パスパラメータ:**

| パラメータ | 必須 | 型 | 説明 |
|---|---|---|---|
| `id` | 必須 | `string` | 会話 ID |

**Request Body:**

| パラメータ | 必須 | 型 | 説明 |
|---|---|---|---|
| `message` | 必須 | `object` | `{"role": "user", "content": "..."}` |

**Response (`data`):**

| フィールド | 型 | 説明 |
|---|---|---|
| `stream_id` | `string` | ストリーム ID |
| `conversation_id` | `string` | 会話 ID |

**エラーケース:**

| コード | 説明 |
|---|---|
| `INVALID_INPUT` | `conversation_id` または `message` が未指定 |
| `NOT_FOUND` | 指定の会話が存在しない |

---

### POST /api/chat/conversations/{id}/export

会話をエクスポートする。内部で `blocks.chat.export_conversation` を呼び出す。

**パスパラメータ:**

| パラメータ | 必須 | 型 | 説明 |
|---|---|---|---|
| `id` | 必須 | `string` | 会話 ID |

**Request Body:**

| パラメータ | 必須 | 型 | 説明 |
|---|---|---|---|
| `format` | 任意 | `string` | `"markdown"` または `"json"`。デフォルト `"markdown"` |

**Response (`data`):**

| フィールド | 型 | 説明 |
|---|---|---|
| `content` | `string` | エクスポートされた文字列 |
| `format` | `string` | フォーマット名 |

**エラーケース:**

| コード | 説明 |
|---|---|
| `NOT_FOUND` | 指定の会話が存在しない |

---

### POST /api/chat/conversations/{id}/summarize

会話を要約してトリミングする。内部で `blocks.chat.summarize_and_trim` を呼び出す。

**パスパラメータ:**

| パラメータ | 必須 | 型 | 説明 |
|---|---|---|---|
| `id` | 必須 | `string` | 会話 ID |

**Request Body:**

| パラメータ | 必須 | 型 | 説明 |
|---|---|---|---|
| `start_message_id` | 必須 | `string` | 要約開始メッセージ ID |
| `end_message_id` | 必須 | `string` | 要約終了メッセージ ID |
| `model` | 任意 | `string` | 要約に使うモデル |

**Response (`data`):** 要約結果オブジェクト

**エラーケース:**

| コード | 説明 |
|---|---|
| `NOT_FOUND` | 会話またはメッセージが存在しない |
| `INVALID_INPUT` | 必須パラメータ不足 |

---

### POST /api/chat/conversations/{id}/auto-trim

会話を自動トリミングする。内部で `blocks.chat.auto_trim` を呼び出す。

**パスパラメータ:**

| パラメータ | 必須 | 型 | 説明 |
|---|---|---|---|
| `id` | 必須 | `string` | 会話 ID |

**Request Body:**

| パラメータ | 必須 | 型 | 説明 |
|---|---|---|---|
| `max_tokens` | 任意 | `int` | トリミング閾値トークン数 |
| `model` | 任意 | `string` | 要約に使うモデル |

**Response (`data`):** トリミング結果オブジェクト

**エラーケース:**

| コード | 説明 |
|---|---|
| `NOT_FOUND` | 会話が存在しない |

---

## Agent — エージェント実行

### POST /api/agent/execute

エージェントタスクを実行する。内部で `blocks.agent.execute` を呼び出す。

**Request Body:**

| パラメータ | 必須 | 型 | 説明 |
|---|---|---|---|
| `task` | 必須 | `string` | 実行するタスクの説明 |
| `tools` | 任意 | `array` | 使用可能なツール定義 |
| `model` | 任意 | `string` | 使用モデル。デフォルト `"default"` |
| `system_prompt` | 任意 | `string` | システムプロンプト |

**Response (`data`):**

| フィールド | 型 | 説明 |
|---|---|---|
| `execution_id` | `string` | 実行 ID |
| `status` | `string` | 実行状態 |
| `steps` | `array` | 実行ステップ一覧 |

**エラーケース:**

| コード | 説明 |
|---|---|
| `ERROR` | `task` が未指定 |

---

### POST /api/agent/{id}/approve

エージェントの現在のステップを承認する。内部で `blocks.agent.approve` を呼び出す。

**パスパラメータ:**

| パラメータ | 必須 | 型 | 説明 |
|---|---|---|---|
| `id` | 必須 | `string` | execution_id |

**Response (`data`):** 承認結果オブジェクト

**エラーケース:**

| コード | 説明 |
|---|---|
| `ERROR` | `execution_id` が未指定または実行が存在しない |

---

### POST /api/agent/{id}/reject

エージェントの現在のステップを拒否する。内部で `blocks.agent.reject` を呼び出す。

**パスパラメータ:**

| パラメータ | 必須 | 型 | 説明 |
|---|---|---|---|
| `id` | 必須 | `string` | execution_id |

**Response (`data`):** 拒否結果オブジェクト

**エラーケース:**

| コード | 説明 |
|---|---|
| `ERROR` | `execution_id` が未指定または実行が存在しない |

---

### POST /api/agent/{id}/cancel

エージェントの実行をキャンセルする。内部で `blocks.agent.cancel` を呼び出す。

**パスパラメータ:**

| パラメータ | 必須 | 型 | 説明 |
|---|---|---|---|
| `id` | 必須 | `string` | execution_id |

**Response (`data`):** キャンセル結果オブジェクト

**エラーケース:**

| コード | 説明 |
|---|---|
| `ERROR` | `execution_id` が未指定または実行が存在しない |

---

### GET /api/agent/{id}/status

エージェントの実行状態を取得する。内部で `blocks.agent.status` を呼び出す。

**パスパラメータ:**

| パラメータ | 必須 | 型 | 説明 |
|---|---|---|---|
| `id` | 必須 | `string` | execution_id |

**Response (`data`):** ステータスオブジェクト

**エラーケース:**

| コード | 説明 |
|---|---|
| `ERROR` | `execution_id` が未指定または実行が存在しない |

---

### POST /api/agent/{id}/instruct

実行中のエージェントにランタイム指示を追加する。内部で `blocks.agent.add_instruction` を呼び出す。指示は次の AI completion ステップの前にメッセージ履歴へ注入される。

**パスパラメータ:**

| パラメータ | 必須 | 型 | 説明 |
|---|---|---|---|
| `id` | 必須 | `string` | execution_id（パスから `execution_id` として注入） |

**Request Body:**

| パラメータ | 必須 | 型 | 説明 |
|---|---|---|---|
| `instruction` | 必須 | `string` | 追加する指示内容 |
| `priority` | 任意 | `string` | `"normal"` または `"urgent"`。デフォルト `"normal"` |

**Response (`data`):**

| フィールド | 型 | 説明 |
|---|---|---|
| `instruction_id` | `string` | 指示 ID (UUID) |
| `execution_id` | `string` | 実行 ID |
| `priority` | `string` | 優先度 |
| `status` | `string` | `"queued"` |

**エラーケース:**

| コード | 説明 |
|---|---|
| `ERROR` | `execution_id` が未指定、`instruction` が未指定、実行が存在しない、または実行がアクティブ状態でない |

---

## Team Workspace Compatibility — legacy multi-agent endpoints

### POST /api/agent/multi/execute

互換性エンドポイント。内部では `CompanySlackRuntime` に会社メッセージを投稿し、mentions/tasks/AgentEngine runs として非同期にルーティングする。レスポンスには `deprecation_warning` が含まれる。

**Request Body:**

| パラメータ | 必須 | 型 | 説明 |
|---|---|---|---|
| `task` | 必須 | `string` | タスクの記述 |
| `agents` | 必須 | `array[object]` | エージェント定義のリスト（最低1つ）。各要素は `{name, role, model?, system_prompt?, tools?}` |
| `company_id` | 任意 | `string` | ルーティング先 team workspace。未指定時は default team workspace |

**Response (`data`):**

| フィールド | 型 | 説明 |
|---|---|---|
| `session_id` | `string` | 互換 session id。実体は company thread id |
| `status` | `string` | ルーティング状態 |
| `turn_results` | `array` | 互換用の空配列 |
| `result` | `object` | CompanySlackRuntime routing result |
| `deprecation_warning` | `string` | 互換 wrapper 告知 |

**エラーケース:**

| コード | 説明 |
|---|---|
| `ERROR` | `task` が未指定、または team workspace routing に失敗 |

---

### GET /api/agent/multi/{id}/status

互換 session id に対応する company thread の messages/tasks を取得する。パスパラメータ `{id}` が `session_id` として注入される。

**パスパラメータ:**

| パラメータ | 必須 | 型 | 説明 |
|---|---|---|---|
| `id` | 必須 | `string` | session_id |

**Response (`data`):** company thread status, messages, tasks, and compatibility warning.

**エラーケース:**

| コード | 説明 |
|---|---|
| `ERROR` | `session_id` が未指定 |

---

### POST /api/agent/multi/{id}/message

互換 session thread にメッセージを投稿する。mention は active run への runtime instruction、または AgentEngine delegated task として処理される。

**パスパラメータ:**

| パラメータ | 必須 | 型 | 説明 |
|---|---|---|---|
| `id` | 必須 | `string` | session_id |

**Request Body:**

| パラメータ | 必須 | 型 | 説明 |
|---|---|---|---|
| `message` | 必須 | `string` | 投入するメッセージ内容 |
| `target_agent` | 任意 | `string` | 特定のエージェント宛にする場合の名前。未指定の場合は共有メッセージとして全エージェントに送信 |

**Response (`data`):**

| フィールド | 型 | 説明 |
|---|---|---|
| `session_id` | `string` | セッション ID |
| `message` | `string` | `"Message injected successfully"` |

**エラーケース:**

| コード | 説明 |
|---|---|
| `ERROR` | `session_id` が未指定、`message` が未指定、またはセッションが存在しない |

---

## Consent — 同意管理

### POST /api/consent/check

テキストがセンシティブかどうか判定する。内部で `blocks.tool.consent_check` を呼び出す。

**Request Body:**

| パラメータ | 必須 | 型 | 説明 |
|---|---|---|---|
| `text` | 必須 | `string` | 判定対象テキスト |
| `use_ai` | 任意 | `bool` | AI 判定を使うか。デフォルト `false` |
| `model` | 任意 | `string` | AI 判定時のモデル指定。デフォルト `"stub/default"` |

**Response (`data`):**

| フィールド | 型 | 説明 |
|---|---|---|
| `requires_consent` | `bool` | 同意が必要かどうか |
| `categories` | `array[string]` | 検出されたカテゴリ |
| `consent_id` | `string \| null` | 同意が必要な場合の同意 ID |
| `disclaimers` | `object` | カテゴリごとの免責テキスト `{category: disclaimer_text}` |

**エラーケース:**

| コード | 説明 |
|---|---|
| `MISSING_PARAM` | `text` が未指定 |
| `INVALID_PARAM` | `text` が文字列でない |

---

### POST /api/consent/{id}/confirm

同意または拒否を記録する。内部で `blocks.tool.consent_confirm` を呼び出す。パスパラメータ `{id}` が `consent_id` として注入される。

**パスパラメータ:**

| パラメータ | 必須 | 型 | 説明 |
|---|---|---|---|
| `id` | 必須 | `string` | consent_id |

**Request Body:**

| パラメータ | 必須 | 型 | 説明 |
|---|---|---|---|
| `accepted` | 必須 | `bool` | ユーザーが同意したかどうか |

**Response (`data`):**

| フィールド | 型 | 説明 |
|---|---|---|
| `consent_id` | `string` | 同意 ID |
| `accepted` | `bool` | 同意状態 |
| `accepted_at` | `string \| null` | 同意した場合の ISO 8601 タイムスタンプ |

**エラーケース:**

| コード | 説明 |
|---|---|
| `MISSING_PARAM` | `consent_id` または `accepted` が未指定 |
| `INVALID_PARAM` | `consent_id` が文字列でない、または `accepted` が bool でない |
| `NOT_FOUND` | 指定の consent_id が存在しない |

---

## Prompt — プロンプト管理

### PUT /api/prompts/{name}

既存プロンプトを更新する。内部で `blocks.prompt.update` を呼び出す。

**パスパラメータ:**

| パラメータ | 必須 | 型 | 説明 |
|---|---|---|---|
| `name` | 必須 | `string` | プロンプト名 |

**Request Body:**

| パラメータ | 必須 | 型 | 説明 |
|---|---|---|---|
| `content` | 任意 | `string` | 新しい本文（`body` のエイリアス） |
| `body` | 任意 | `string` | 新しい本文 |
| `description` | 任意 | `string` | 説明 |
| `variables` | 任意 | `array` | 変数定義 |
| `metadata` | 任意 | `object` | メタデータ |

**Response (`data`):** 更新後のプロンプトオブジェクト

**エラーケース:**

| コード | 説明 |
|---|---|
| `NOT_FOUND` | 指定のプロンプトが存在しない |

---

### DELETE /api/prompts/{name}

プロンプトを削除する。内部で `blocks.prompt.delete` を呼び出す。

**パスパラメータ:**

| パラメータ | 必須 | 型 | 説明 |
|---|---|---|---|
| `name` | 必須 | `string` | プロンプト名 |

**Response (`data`):** `{"deleted": true}`

**エラーケース:**

| コード | 説明 |
|---|---|
| `NOT_FOUND` | 指定のプロンプトが存在しない |

---

### POST /api/prompts/convert

tool ↔ prompt の相互変換を行う。内部で `blocks.prompt.convert` を呼び出す。

**Request Body:**

| パラメータ | 必須 | 型 | 説明 |
|---|---|---|---|
| `source_type` | 必須 | `string` | `"tool"` または `"prompt"` |
| `source_name` | 必須 | `string` | 変換元の名前 |
| `target_type` | 必須 | `string` | `"tool"` または `"prompt"` |

**Response (`data`):**

| フィールド | 型 | 説明 |
|---|---|---|
| `result` | `object` | 変換結果（tool 定義またはプロンプトオブジェクト） |
| `target_type` | `string` | 変換先タイプ |

**エラーケース:**

| コード | 説明 |
|---|---|
| `INVALID_INPUT` | `source_type`/`target_type` が不正、または同一 |
| `NOT_FOUND` | 変換元が存在しない |

---

## Tool — 動的ツール管理

### POST /api/tools/create

動的ツールを作成する。handler_code が未指定の場合は AI で自動生成する。内部で `blocks.tool.create` を呼び出す。

**Request Body:**

| パラメータ | 必須 | 型 | 説明 |
|---|---|---|---|
| `name` | 必須 | `string` | ツール名（tool_id と同一） |
| `description` | 任意 | `string` | ツールの説明 |
| `parameters` | 必須 | `object` | JSON Schema 形式のパラメータ定義 |
| `handler_code` | 任意 | `string` | Python handler コード。null なら AI 生成 |
| `tags` | 任意 | `array[string]` | タグ。デフォルト `["dynamic", "user-created"]` |
| `model` | 任意 | `string` | handler_code 生成に使う AI モデル |

**Response (`data`):**

| フィールド | 型 | 説明 |
|---|---|---|
| `tool_id` | `string` | ツール ID |
| `name` | `string` | ツール名 |
| `summary` | `string` | 説明 |
| `handler_code` | `string` | 生成されたハンドラコード |
| `created_at` | `string` | ISO 8601 タイムスタンプ |

**エラーケース:**

| コード | 説明 |
|---|---|
| `MISSING_PARAM` | `name` または `parameters` が未指定 |
| `INVALID_PARAM` | `parameters` が dict でない |
| `ALREADY_EXISTS` | 同名のツールが既に存在する |
| `REGISTER_ERROR` | 登録処理でエラー |

---

### PUT /api/tools/{name}

動的ツールを更新する。内部で `blocks.tool.update` を呼び出す。

**パスパラメータ:**

| パラメータ | 必須 | 型 | 説明 |
|---|---|---|---|
| `name` | 必須 | `string` | ツール名 |

**Request Body:**

| パラメータ | 必須 | 型 | 説明 |
|---|---|---|---|
| `description` | 任意 | `string` | 新しい説明 |
| `parameters` | 任意 | `object` | 新しいスキーマ |
| `handler_code` | 任意 | `string` | 新しいハンドラコード |
| `tags` | 任意 | `array[string]` | 新しいタグ |

**Response (`data`):** 更新後のツール定義

**エラーケース:**

| コード | 説明 |
|---|---|
| `NOT_FOUND` | 指定のツールが存在しないか dynamic でない |

---

### DELETE /api/tools/{name}

動的ツールを削除する。ファイルも同時に削除される。内部で `blocks.tool.delete` を呼び出す。

**パスパラメータ:**

| パラメータ | 必須 | 型 | 説明 |
|---|---|---|---|
| `name` | 必須 | `string` | ツール名 |

**Response (`data`):** `{"deleted": true}`

**エラーケース:**

| コード | 説明 |
|---|---|
| `NOT_FOUND` | 指定のツールが存在しないか dynamic でない |

---

### GET /api/tools/{name}/export

ツール定義を handler_code 込みでエクスポートする。内部で `blocks.tool.export` を呼び出す。

**パスパラメータ:**

| パラメータ | 必須 | 型 | 説明 |
|---|---|---|---|
| `name` | 必須 | `string` | ツール名 |

**Response (`data`):** ツール定義オブジェクト（handler_code フィールドを含む）

**エラーケース:**

| コード | 説明 |
|---|---|
| `NOT_FOUND` | 指定のツールが存在しない |

---

## Dev — 開発者ツール

### GET /api/dev/inspect

直前のリクエスト情報を取得する。内部で `blocks.dev.inspect` を呼び出す。

**Request Body (任意):**

| パラメータ | 必須 | 型 | 説明 |
|---|---|---|---|
| `request_id` | 任意 | `string` | 特定のリクエスト ID |
| `conversation_id` | 任意 | `string` | 特定の会話 ID |

`request_id` 指定時はそのログを返す。`conversation_id` 指定時はその会話の最新ログを返す。両方未指定で直前のリクエストを返す。

**Response (`data`):**

| フィールド | 型 | 説明 |
|---|---|---|
| `request_id` | `string` | リクエスト ID |
| `conversation_id` | `string` | 会話 ID |
| `model` | `string` | 使用モデル |
| `prompt_used` | `string` | 使用されたプロンプト |
| `tools_called` | `array` | 呼び出されたツール |
| `context_info` | `object` | コンテキスト情報 |
| `timestamp` | `string` | ISO 8601 タイムスタンプ |

**エラーケース:**

| コード | 説明 |
|---|---|
| `NOT_FOUND` | 指定の ID のログが存在しない |

---

### GET /api/dev/prompt-history

プロンプト履歴を取得する。内部で `blocks.dev.prompt_history` を呼び出す。

**Request Body (任意):**

| パラメータ | 必須 | 型 | 説明 |
|---|---|---|---|
| `limit` | 任意 | `int` | 取得件数。デフォルト 20 |

**Response (`data`):** ログ配列（新しい順）

---

### POST /api/dev/edit-prompt

プロンプトをライブ編集して再実行する。内部で `blocks.dev.edit_prompt_live` を呼び出す。

**Request Body:**

| パラメータ | 必須 | 型 | 説明 |
|---|---|---|---|
| `request_id` | 必須 | `string` | 編集対象のリクエスト ID |
| `new_prompt` | 必須 | `string` | 新しいプロンプト |

**Response (`data`):** 再実行結果

**エラーケース:**

| コード | 説明 |
|---|---|
| `NOT_FOUND` | 指定のリクエストが存在しない |
| `INVALID_INPUT` | 必須パラメータ不足 |

---

### POST /api/dev/replay

過去のリクエストを再実行する。内部で `blocks.dev.replay` を呼び出す。

**Request Body:**

| パラメータ | 必須 | 型 | 説明 |
|---|---|---|---|
| `request_id` | 必須 | `string` | 再実行対象のリクエスト ID |
| `model` | 任意 | `string` | 別のモデルで再実行 |

**Response (`data`):** 再実行結果

**エラーケース:**

| コード | 説明 |
|---|---|
| `NOT_FOUND` | 指定のリクエストが存在しない |

---

## System — システム情報

### GET /health

canonical Host の process liveness。UI bootstrap や business data の readiness には使わない。

**Request Body:** なし

**Response (`data`):**

| フィールド | 型 | 説明 |
|---|---|---|
| `status` | `string` | `"ok"` または `"error"` |
| `runtime_status` | `string` | Host lifecycle state |
| `runtime_ready` | `boolean` | captured runtime lifecycle state |

**エラーケース:** なし

---

### GET /ui-readiness

Launcher-bound request proof または panel session で認証された、bounded UI bootstrap
assessment。認証前に deep probe は実行されない。

**Response (`data`):**

| フィールド | 型 | 説明 |
|---|---|---|
| `schema` | `string` | `io.tobkiri.ui-readiness.v1` |
| `status` | `string` | `UP`, `DEGRADED`, `DOWN` |
| `ready` | `boolean` | surface を開けるときだけ `true` |
| `profile_id` | `string` | captured v4 Profile identity（capture がある場合） |
| `plan_digest` | `string` | captured ResolvedPlan digest（capture がある場合） |
| `contract_map_digest` | `string` | exact route/target snapshot digest |
| `probes` | `object` | 九つの required named probe と safe status/code/message/duration |

required probe は `static_bundle`, `chat_route`, `ui_catalog`, `settings`,
`model_catalog`, `tool_catalog`, `auth_session`, `conversation_bootstrap`,
`default_conversation_load`。credential、cookie、Broker/provider payload は返さない。

**エラーケース:** 認証されていない request は `401 Unauthorized`。missing/stale/timeout
dependency は HTTP success envelope 内の該当 named probe を `DOWN` にする。
UI readiness request/response proof と legacy desktop liveness proof は別々の
domain-derived key を使用し、`/health` を readiness signing oracle として利用できない。
generated Frontend Contract Map に未公開の required bootstrap API は
`BOOTSTRAP_ROUTE_MISSING` となり、別 operation や legacy route へ fallback しない。

---

### GET /api/context

Pack のコンテキスト情報を取得する。facade が設定されている場合はインターフェース一覧も返す。

**Request Body:** なし

**Response (`data`):**

| フィールド | 型 | 説明 |
|---|---|---|
| `pack` | `string` | `"defaults"` |
| `interfaces` | `object` | カーネルファサードのインターフェース一覧 |
| `ts` | `string` | ISO 8601 タイムスタンプ |

**エラーケース:** なし

---

## Static — 静的ファイル配信

### GET /

Shell HTML を返す。`ui/shell.html` が存在すればその内容を返す。存在しなければフォールバック HTML を返す。

**Response:** `text/html` コンテンツ

---

### GET /static/{path}

静的ファイルを配信する。`..` による親ディレクトリ参照はブロックされる。

**パスパラメータ:**

| パラメータ | 必須 | 型 | 説明 |
|---|---|---|---|
| `path` | 必須 | `string` | ファイルの相対パス |

**Response:** 対応する Content-Type のファイル内容。バイナリファイルは base64 エンコードされる。

対応する拡張子: `.html`, `.css`, `.js`, `.json`, `.png`, `.jpg`, `.jpeg`, `.gif`, `.svg`, `.ico`

**エラーケース:**

| コード | 説明 |
|---|---|
| `ERROR` | パスが不正（`..` を含む等）またはファイルが存在しない |
