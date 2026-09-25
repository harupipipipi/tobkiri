```markdown
# api.md — Rumi AI OS API & Transport 設計書

## 1. 概要

defaults は外部からの通信を受け付ける「仕組み」を提供する。具体的なエンドポイント（チャット、モデル一覧等）は Flow の api トリガーが登録する。defaults はルーティング、認証、ストリーミング、エラー形式の基盤だけを定義し、何ができるかは user_data 側の Flow 定義で決まる。

Tauri フロントエンド、CLI、外部スクリプト、Webhook、全てが同じ Flow に到達する。違うのは transport（メッセージの運び方）だけである。


## 2. 設計思想

**Transport 非依存**: 全ての通信は最終的に `FlowEngine.execute(flow_id, trigger_input)` の呼び出しになる。HTTP、stdin/stdout、UDS のいずれを経由しても、Flow に届く trigger_input は同一形式である。

**エンドポイントはフローである**: HTTP の `/v1/chat/completions` も CLI の `rumi chat` も、同じ `default.chat` フローを起動する。エンドポイントの追加は Flow 定義の追加であり、コード変更を伴わない。

**認証は transport 層で行う**: HTTP はAPIキー、stdio は親プロセスの信頼、UDS はソケットパーミッション。Flow 層は認証済みリクエストだけを受け取る。

**ストリーミングは transport が吸収する**: Flow は `ctx.emit()` でイベントを発行するだけ。HTTP transport は SSE に変換し、stdio transport は JSON Lines に変換し、Tauri transport は IPC Channel に変換する。Flow はどの transport で配信されるかを知らない。


## 3. アーキテクチャ

```
外部            transport 層               Flow Engine
───────────    ───────────────────        ─────────────
HTTP Client ─→ http transport ─┐
                               ├──→ router ──→ FlowEngine.execute()
CLI         ─→ stdio transport ┤                    ↓
                               │              Flow handler.py
Tauri       ─→ stdin/stdout ───┤                    ↓
                               │              ctx.emit() ──→ transport が配信
UDS Client  ─→ uds transport ──┘
```

transport 層は defaults の handler として実装される。

| handler | transport | 権限 |
|---|---|---|
| `defaults.transport.http` | HTTP サーバー | `frontend.serve`, `frontend.bind` |
| `defaults.transport.stdio` | 標準入出力 | `frontend.serve` |
| `defaults.transport.uds` | Unix Domain Socket | `frontend.serve`, `frontend.bind` |

各 transport handler は Grant で許可された権限のみで動作し、カーネルオブジェクト全体にはアクセスしない（io.http.server の問題を回避）。


## 4. 共通メッセージ形式

全 transport で共通の JSON 形式。transport はこの形式をそのまま流すか、自身のプロトコルに変換する。

### 4.1 リクエスト

```json
{
  "id": "req_01JXYZ",
  "flow_id": "default.chat",
  "input": {
    "model": "anthropic/claude-sonnet-4",
    "messages": [
      {"role": "user", "content": "hello"}
    ],
    "stream": true
  },
  "config": {}
}
```

| フィールド | 型 | 必須 | 説明 |
|---|---|---|---|
| id | string | 任意 | リクエスト識別子。省略時は自動生成 |
| flow_id | string | 必須 | 起動する Flow の ID |
| input | object | 必須 | Flow の trigger_input |
| config | object | 任意 | Flow の flow_config 上書き |

### 4.2 レスポンス（非ストリーミング）

```json
{
  "id": "req_01JXYZ",
  "status": "completed",
  "output": {
    "content": "Hello! How can I help you?",
    "model": "claude-sonnet-4-20250514",
    "usage": {
      "input_tokens": 12,
      "output_tokens": 8,
      "total_tokens": 20
    },
    "finish_reason": "stop"
  },
  "metadata": {
    "flow_id": "default.chat",
    "duration_ms": 1200
  }
}
```

| フィールド | 型 | 説明 |
|---|---|---|
| id | string | リクエスト識別子 |
| status | string | `completed`, `error`, `cancelled`, `timeout` |
| output | object | Flow の出力（FlowResult.output） |
| metadata | object | 実行メタデータ |

### 4.3 レスポンス（ストリーミング）

ストリーミング時は transport に応じた方式で複数のイベントが配信される。各イベントの形式は共通。

```json
{"event": "stream.start", "id": "req_01JXYZ", "data": {}}
{"event": "stream.delta", "id": "req_01JXYZ", "data": {"type": "content_delta", "content": "Hello"}}
{"event": "stream.delta", "id": "req_01JXYZ", "data": {"type": "content_delta", "content": "!"}}
{"event": "stream.delta", "id": "req_01JXYZ", "data": {"type": "tool_call_start", "id": "tc_01", "name": "file_read"}}
{"event": "stream.delta", "id": "req_01JXYZ", "data": {"type": "tool_call_delta", "arguments_chunk": "{\"path\":"}}
{"event": "stream.delta", "id": "req_01JXYZ", "data": {"type": "tool_call_end", "id": "tc_01"}}
{"event": "stream.delta", "id": "req_01JXYZ", "data": {"type": "thinking_delta", "content": "Let me think..."}}
{"event": "stream.end", "id": "req_01JXYZ", "data": {"status": "completed", "output": {...}, "metadata": {...}}}
```

stream.delta の `data.type` は ai_client.md セクション 11.3 の正規化イベント一覧と同一。transport 層はこのイベント列をプロトコル固有の方式に変換する。

### 4.4 エラー

```json
{
  "id": "req_01JXYZ",
  "status": "error",
  "error": {
    "type": "flow_not_found",
    "message": "Flow 'nonexistent' is not registered",
    "code": "FLOW_NOT_FOUND",
    "details": {}
  }
}
```

エラーコードの体系。

| コード | 説明 |
|---|---|
| `AUTH_REQUIRED` | 認証が必要 |
| `AUTH_INVALID` | APIキーが無効 |
| `FLOW_NOT_FOUND` | 指定された flow_id が存在しない |
| `FLOW_ERROR` | Flow 実行中のエラー |
| `FLOW_TIMEOUT` | Flow がタイムアウト |
| `FLOW_CANCELLED` | ユーザーまたはシステムがキャンセル |
| `VALIDATION_ERROR` | input の形式が不正 |
| `PERMISSION_DENIED` | 権限不足 |
| `RATE_LIMITED` | レート制限 |
| `INTERNAL_ERROR` | 内部エラー |


## 5. HTTP Transport

### 5.1 起動

`defaults.transport.http` handler がHTTPサーバーを起動する。設定は user_data で行う。

```json
// user_data/config/transport.json
{
  "http": {
    "enabled": true,
    "host": "127.0.0.1",
    "port": 3000,
    "cors": {
      "enabled": false,
      "origins": []
    }
  }
}
```

### 5.2 認証

HTTP transport はリクエストごとに認証を行う。

```
Authorization: Bearer rumi_xxxxxxxxxxxx
```

APIキーは `user_data/secrets/api_keys.json` で管理する。

Settings の provider credential status は、credential の存在と直近の
provider usability を別々に表示する。v4 Credential Broker が所有する
provider-default credential は opaque handle の存在だけを readonly / masked
metadata として投影し、環境変数名、値、長さ、prefix、handle 自体は返さない。
`present_unverified` は利用可能を意味せず、実行時には Authority Kernel に
binding された provider adapter が scope、account、quota、region と現在の
provider health を検証する。health contract が未起動または stale の場合も
起動を block せず、Settings は `unknown` / `unknown_stale` として保守的に表示する。

```json
{
  "keys": [
    {
      "id": "key_01",
      "key_hash": "sha256:abc...",
      "name": "My API Key",
      "created_at": "2026-02-14T10:00:00Z",
      "permissions": ["*"],
      "rate_limit": {
        "requests_per_minute": 60
      }
    }
  ]
}
```

`permissions` は Flow レベルの許可。`["*"]` は全 Flow にアクセス可能。`["default.chat", "default.model_list"]` のように制限できる。

### 5.3 ルーティング

HTTP エンドポイントから flow_id へのマッピングは `user_data/config/routes.json` で定義する。

```json
{
  "routes": [
    {
      "method": "POST",
      "path": "/v1/chat/completions",
      "flow_id": "default.chat",
      "input_mapping": {
        "model": "body.model",
        "messages": "body.messages",
        "stream": "body.stream",
        "temperature": "body.temperature",
        "max_tokens": "body.max_tokens",
        "tools": "body.tools"
      },
      "description": "OpenAI 互換チャット API"
    },
    {
      "method": "GET",
      "path": "/v1/models",
      "flow_id": "default.model_list",
      "input_mapping": {},
      "description": "モデル一覧"
    },
    {
      "method": "POST",
      "path": "/v1/flows/{flow_id}/run",
      "flow_id": "{flow_id}",
      "input_mapping": {
        "*": "body"
      },
      "description": "任意の Flow を直接実行"
    }
  ]
}
```

`input_mapping` はHTTPリクエストから共通メッセージ形式の `input` へのフィールドマッピング。`body.model` はリクエストボディの `model` フィールドを意味する。`"*": "body"` はボディ全体を input にする。`{flow_id}` はパスパラメータ。

routes.json を編集するだけで任意のHTTPエンドポイントを追加できる。Flow 側のコード変更は不要。Pack が routes.json にエントリを追加することも可能。

### 5.4 リクエスト処理フロー

```
HTTP リクエスト受信
  ↓
認証（Authorization ヘッダー → api_keys.json 照合）
  → 失敗: 401 AUTH_REQUIRED / AUTH_INVALID
  ↓
ルーティング（method + path → routes.json 照合）
  → 失敗: 404 FLOW_NOT_FOUND
  ↓
input_mapping でリクエストボディを共通形式に変換
  ↓
APIキーの permissions で flow_id へのアクセス権を確認
  → 失敗: 403 PERMISSION_DENIED
  ↓
レート制限チェック
  → 超過: 429 RATE_LIMITED
  ↓
FlowEngine.execute(flow_id, input)
  ↓
stream == false:
  → Flow 完了まで待機 → JSON レスポンス返却
  → Content-Type: application/json

stream == true:
  → SSE ストリーム開始
  → Content-Type: text/event-stream
  → Flow の ctx.emit() イベントを SSE イベントに変換して逐次送信
  → Flow 完了時に stream.end を送信して接続を閉じる
```

### 5.5 SSE ストリーミング

```
HTTP/1.1 200 OK
Content-Type: text/event-stream
Cache-Control: no-cache

event: stream.start
data: {"id":"req_01JXYZ","data":{}}

event: stream.delta
data: {"id":"req_01JXYZ","data":{"type":"content_delta","content":"Hello"}}

event: stream.delta
data: {"id":"req_01JXYZ","data":{"type":"content_delta","content":"!"}}

event: stream.end
data: {"id":"req_01JXYZ","data":{"status":"completed","output":{...}}}

```

### 5.6 OpenAI 互換モード

routes.json の `/v1/chat/completions` エンドポイントに `response_format: "openai"` を追加すると、レスポンスを OpenAI API 互換形式に変換して返す。

```json
{
  "method": "POST",
  "path": "/v1/chat/completions",
  "flow_id": "default.chat",
  "input_mapping": {
    "model": "body.model",
    "messages": "body.messages",
    "stream": "body.stream",
    "temperature": "body.temperature",
    "max_tokens": "body.max_tokens",
    "tools": "body.tools"
  },
  "response_format": "openai"
}
```

変換は transport 層が行う。Flow の出力（共通形式）を OpenAI のレスポンス形式にマッピングする。

```json
// 共通形式からの変換
{
  "id": "chatcmpl-xxxx",
  "object": "chat.completion",
  "created": 1709000000,
  "model": "claude-sonnet-4-20250514",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "Hello!"
      },
      "finish_reason": "stop"
    }
  ],
  "usage": {
    "prompt_tokens": 12,
    "completion_tokens": 8,
    "total_tokens": 20
  }
}
```

ストリーミング時も OpenAI 互換 SSE 形式に変換する。これにより既存の OpenAI SDK やライブラリがそのまま rumi に接続できる。

### 5.7 External Input Intake

External input routes are provider adapters, not chat runtime contracts. They
should normalize inbound provider payloads into `ExternalEvent`, evaluate
`AudiencePolicy`, select an `InputProfile`, call `submit_input`, and return or
send a response through `ResponsePlanner` plus `ResponseAdapter`.

Current defaultspack HTTP routes include:

| method | path | role |
|---|---|---|
| `GET` | `/api/integrations/secrets` | secret status only, no raw values |
| `POST` | `/api/integrations/secrets` | write-only set or clear for supported provider secrets |
| `POST` | `/api/integrations/slack/events` | Slack event intake |
| `POST` | `/api/integrations/line/webhook` | LINE webhook intake |
| `POST` | `/api/integrations/discord/interactions` | Discord interaction intake |
| `POST` | `/api/integrations/discord/events` | Discord event intake |
| `GET` | `/api/external/tokens` | masked external token status |
| `POST` | `/api/external/tokens` | write-only external token upsert, rename, delete |
| `POST` | `/api/webhooks/inbound/{webhook_id}` | generic webhook intake |
| `GET` | `/api/webhooks/endpoints` | list webhook endpoint configs |
| `POST` | `/api/webhooks/endpoints` | create webhook endpoint config |
| `PUT` | `/api/webhooks/endpoints/{webhook_id}` | update webhook endpoint config |
| `DELETE` | `/api/webhooks/endpoints/{webhook_id}` | delete webhook endpoint config |
| `POST` | `/api/webhooks/endpoints/{webhook_id}/test` | run a webhook test payload |
| `GET` | `/api/webhooks/public-urls` | list public URL providers and the local default URL |
| `POST` | `/api/webhooks/public-urls` | create a provider-backed public URL; failures return redacted `ok: false` data |
| `DELETE` | `/api/webhooks/public-urls/{url_id}` | close or clear a public URL |

Cloudflare Quick Tunnel may supply a temporary public URL for development, but
the API contract must not depend on it. The External Input UI uses it only to
generate a copyable provider webhook URL such as
`https://...trycloudflare.com/api/integrations/line/webhook`. Any tunnel or
hosted ingress should feed the same local routes.


## 6. stdio Transport

### 6.1 概要

stdin/stdout で JSON Lines を送受信する。Tauri のフロントエンドと rumiai プロセス間の通信、および CLI がこの transport を使用する。

### 6.2 形式

stdin（クライアント → rumiai）:

```jsonl
{"id":"req_01","flow_id":"default.chat","input":{"model":"gpt-4o","messages":[{"role":"user","content":"hello"}],"stream":false}}
{"id":"req_02","flow_id":"default.model_list","input":{}}
```

stdout（rumiai → クライアント）:

```jsonl
{"id":"req_01","status":"completed","output":{"content":"Hello!","model":"gpt-4o","usage":{"input_tokens":10,"output_tokens":5,"total_tokens":15},"finish_reason":"stop"},"metadata":{"flow_id":"default.chat","duration_ms":800}}
{"id":"req_02","status":"completed","output":{"models":[...]},"metadata":{"flow_id":"default.model_list","duration_ms":50}}
```

ストリーミング時:

```jsonl
{"event":"stream.start","id":"req_03","data":{}}
{"event":"stream.delta","id":"req_03","data":{"type":"content_delta","content":"Hello"}}
{"event":"stream.end","id":"req_03","data":{"status":"completed","output":{...}}}
```

### 6.3 認証

stdio transport は認証を行わない。stdin/stdout に接続できるのは親プロセス（Tauri の Rust 層、または CLI を起動したシェル）だけであり、プロセス間の信頼で十分とする。

### 6.4 フロントエンドメッセージとの統合

frontend.md で定義されている `component.register`、`render.mount`、`message.send` 等のフロントエンド固有メッセージも同じ stdin/stdout を共有する。区別は `type` フィールドの有無で行う。

```jsonl
// Flow 呼び出し（type フィールドなし、flow_id フィールドあり）
{"id":"req_01","flow_id":"default.chat","input":{...}}

// フロントエンドメッセージ（type フィールドあり）
{"type":"asset.register","data":{"asset_id":"defaults.chat","entry":"ui/chat.html",...}}
{"type":"message.send","component":"defaults.chat","data":{...}}
```

transport handler がメッセージを振り分ける。`flow_id` があれば FlowEngine に転送、`type` があればフロントエンド handler に転送。


## 7. UDS Transport

### 7.1 概要

Unix Domain Socket で通信する。ローカルの別プロセス（他のアプリケーション、スクリプト、エディタプラグイン等）が rumiai と通信する場合に使用する。

### 7.2 設定

```json
// user_data/config/transport.json
{
  "uds": {
    "enabled": true,
    "path": "/tmp/rumiai.sock"
  }
}
```

### 7.3 プロトコル

JSON Lines。stdio transport と同一形式。

### 7.4 認証

ソケットファイルのパーミッション（`0600`、所有者のみ読み書き可能）で制御する。追加でAPIキー認証を要求する設定も可能。

```json
{
  "uds": {
    "enabled": true,
    "path": "/tmp/rumiai.sock",
    "require_auth": true
  }
}
```

`require_auth: true` の場合、接続後の最初のメッセージで認証を行う。

```jsonl
{"type":"auth","key":"rumi_xxxxxxxxxxxx"}
{"type":"auth.result","status":"ok"}
```


## 8. CLI 統合

### 8.1 概要

CLI は stdio transport の上に構築されるシンクライアントである。rumiai プロセスを起動し、stdin/stdout で共通メッセージ形式を送受信する。CLI 自身はドメイン知識を持たない。

### 8.2 コマンド体系

CLI のコマンドは flow_id へのエイリアスである。

```
rumi chat "hello"
  → {"flow_id": "default.chat", "input": {"messages": [{"role": "user", "content": "hello"}], "stream": true}}

rumi models
  → {"flow_id": "default.model_list", "input": {}}

rumi run my_custom_flow --input '{"key": "value"}'
  → {"flow_id": "my_custom_flow", "input": {"key": "value"}}
```

コマンドと flow_id のマッピングは `user_data/config/cli.json` で定義する。

```json
{
  "commands": {
    "chat": {
      "flow_id": "default.chat",
      "input_template": {
        "messages": [{"role": "user", "content": "{args.message}"}],
        "model": "{flags.model}",
        "stream": true
      },
      "flags": {
        "model": {"short": "m", "default": null},
        "no-stream": {"type": "bool", "default": false}
      }
    },
    "models": {
      "flow_id": "default.model_list",
      "input_template": {}
    },
    "agent": {
      "flow_id": "default.agent_chat",
      "input_template": {
        "messages": [{"role": "user", "content": "{args.message}"}],
        "agent_id": "{flags.agent}",
        "stream": true
      },
      "flags": {
        "agent": {"short": "a", "default": "coding_assistant"}
      }
    }
  },
  "aliases": {
    "c": "chat",
    "m": "models",
    "a": "agent"
  }
}
```

Pack が cli.json にコマンドを追加すれば、CLI からその Pack の Flow を呼び出せる。CLI 本体のコード変更は不要。

### 8.3 出力フォーマット

CLI は transport から受け取った共通形式のレスポンスを人間が読める形式に変換する。

非ストリーミング:

```
$ rumi chat "hello" --no-stream
Hello! How can I help you today?

[gpt-4o | 20 tokens | 1.2s]
```

ストリーミング:

```
$ rumi chat "hello"
Hello! How can I help you today?█
```

ストリーミング時は stream.delta のテキストチャンクを逐次出力する。カーソルが末尾に表示される。stream.end 受信で完了。

### 8.4 Widget のテキストフォールバック

Flow やツールが emit_widget で Widget JSON を送出した場合、CLI は Widget のテキスト表現にフォールバックする。

| Widget type | CLI 表示 |
|---|---|
| Text | そのままテキスト出力 |
| CodeBlock | ``` 囲み + 言語名 |
| Diff | unified diff 形式 |
| Image | `[Image: alt WxH]` |
| Screenshot | `[Screenshot: url]` |
| Terminal | コマンドと出力をそのまま |
| Progress | `[████░░░░░░] 40%` |
| Table | ASCII テーブル |
| FileTree | インデント付きテキスト |
| Markdown | そのまま出力 |
| Chart | 数値要約 |
| Audio | `[Audio: duration]` |
| Video | `[Video: duration]` |
| Map | `[Map: lat, lng]` |
| Indicator | `● label (state)` |
| Card | ヘッダー + ボディのテキスト結合 |


## 9. エンドポイント登録メカニズム

### 9.1 Flow の api トリガー

Flow 定義の `trigger.type: api` で HTTP エンドポイントが登録される。flow.md のトリガーシステムと連動する。

```yaml
# user_data/shared/flows/my_api/flow.yaml
flow_id: my_custom_api
trigger:
  type: api
  config:
    endpoint: "/v1/my/endpoint"
    method: POST
    auth_required: true

handler: handler.py
```

Flow のデプロイ時に transport.http handler が routes を再読み込みし、このエンドポイントが有効になる。

### 9.2 routes.json との関係

routes.json は静的なルーティング定義。Flow の api トリガーは動的に登録される。解決順序は以下の通り。

1. routes.json の静的ルート（最優先）
2. Flow の api トリガーで登録された動的ルート

同一パスが競合した場合は routes.json が勝つ。

### 9.3 Pack がエンドポイントを追加する

Pack は2つの方法でエンドポイントを追加できる。

方法1: Flow の api トリガーを定義する。

```yaml
# user_data/packs/my_pack/flows/webhook_receiver/flow.yaml
flow_id: my_pack.webhook
trigger:
  type: api
  config:
    endpoint: "/hooks/my_pack"
    method: POST
    auth_required: false
handler: handler.py
```

方法2: routes.json にエントリを追加する（Pack のインストールスクリプト経由）。

いずれの方法でも defaults 側のコード変更は不要。


## 10. セキュリティ

### 10.1 transport 層の隔離

各 transport handler は Grant で許可された権限のみで動作する。`io.http.server` のようにカーネルオブジェクト全体が渡される問題は発生しない。

### 10.2 認証の階層

| transport | 認証方式 | 根拠 |
|---|---|---|
| HTTP | APIキー（Bearer トークン） | ネットワーク経由のアクセスは常に認証が必要 |
| stdio | なし | 親プロセス信頼 |
| UDS | ソケットパーミッション + オプションでAPIキー | ローカルプロセス信頼 + 設定可能 |

### 10.3 Flow レベルの権限

APIキーの `permissions` フィールドで、キーごとにアクセス可能な Flow を制限できる。管理用の強い権限を持つキーと、限定用途のキーを分離できる。

### 10.4 レート制限

transport 層で実施する。APIキーごとに `requests_per_minute` を設定可能。超過時は `429 RATE_LIMITED` を返す。

### 10.5 input のバリデーション

Flow の `config_schema`（flow.md セクション 6.1）による入力バリデーションは FlowEngine が実施する。transport 層は形式チェック（JSON として valid か）のみ行う。


## 11. デフォルトルート

defaults が routes.json に登録するデフォルトルート。全て user_data の routes.json で上書き・削除可能。

```json
{
  "routes": [
    {
      "method": "POST",
      "path": "/v1/chat/completions",
      "flow_id": "default.chat",
      "input_mapping": {
        "model": "body.model",
        "messages": "body.messages",
        "stream": "body.stream",
        "temperature": "body.temperature",
        "max_tokens": "body.max_tokens",
        "tools": "body.tools",
        "tool_choice": "body.tool_choice"
      },
      "response_format": "openai",
      "description": "OpenAI 互換チャット API"
    },
    {
      "method": "GET",
      "path": "/v1/models",
      "flow_id": "default.model_list",
      "input_mapping": {},
      "response_format": "openai",
      "description": "OpenAI 互換モデル一覧"
    },
    {
      "method": "GET",
      "path": "/v1/models/{model_id}",
      "flow_id": "default.model_info",
      "input_mapping": {
        "model_id": "path.model_id"
      },
      "response_format": "openai",
      "description": "OpenAI 互換モデル情報"
    },
    {
      "method": "POST",
      "path": "/v1/flows/{flow_id}/run",
      "flow_id": "{flow_id}",
      "input_mapping": {
        "*": "body"
      },
      "description": "任意の Flow を直接実行"
    },
    {
      "method": "GET",
      "path": "/v1/flows",
      "flow_id": "default.flow_list",
      "input_mapping": {},
      "description": "登録済み Flow の一覧"
    },
    {
      "method": "GET",
      "path": "/health",
      "flow_id": "default.health",
      "input_mapping": {},
      "description": "ヘルスチェック"
    }
  ]
}
```

### 11.1 input_mapping の記法

| 記法 | 説明 | 例 |
|---|---|---|
| `body.{field}` | リクエストボディのフィールド | `body.model` |
| `path.{param}` | URL パスパラメータ | `path.model_id` |
| `query.{param}` | クエリパラメータ | `query.limit` |
| `header.{name}` | リクエストヘッダー | `header.X-Custom` |
| `"*": "body"` | ボディ全体を input にする | — |

### 11.2 response_format

| 値 | 説明 |
|---|---|
| 省略 / `"default"` | 共通メッセージ形式でそのまま返す |
| `"openai"` | OpenAI API 互換形式に変換して返す |

将来的に `"anthropic"` 等を追加可能。変換ロジックは transport 層に配置する。


## 12. 他ドキュメントとの関係

| ドキュメント | 関係 |
|---|---|
| frontend.md | stdio transport のメッセージ形式は frontend.md の通信フローと共存する。`flow_id` があれば FlowEngine へ、`type` があればフロントエンドへ |
| flow.md | エンドポイントは Flow の api トリガーで登録される。FlowEngine がリクエストを処理する |
| ai_client.md | ストリーミングイベントの type は ai_client.md セクション 11.3 の正規化イベントと同一 |
| tool.md | ツールの context API（call_handler, emit_event 等）で transport を操作することはない。transport は上位層であり、ツールから直接触らない |
| external-inputs.md | Defines the provider-neutral intake path: `ExternalEvent`, `AudiencePolicy`, `InputProfile`, `submit_input`, `ResponsePlanner`, and `ResponseAdapter` |
| webhooks.md | Documents webhook-specific verification and ack behavior before normalized submission |
```
