# Getting Started

rumiai defaults Pack のセットアップから最初の会話を送信するまでのガイドです。

## 前提条件

- **Python 3.11 以上** がインストールされていること
- **rumiai カーネル** がセットアップ済みであること（`https://github.com/harupipipipi/rumiai` の `tobkiri_runtime/` 配下）
- **git** がインストールされていること

## インストール

### 1. defaults Pack のクローン

```bash
git clone https://github.com/harupipipipi/rumiai_defaults.git
```

### 2. カーネルへの登録

rumiai カーネルの Pack 登録ディレクトリに defaults Pack のパスを設定します。カーネルの `ecosystem/` ディレクトリ、または設定ファイルで defaults Pack のルートパスを指定してください。defaults Pack のルートには `ecosystem.json` が含まれており、カーネルはこのファイルを読み取って Pack を認識します。

```
ecosystem.json   ← カーネルが読み取る Pack 構造定義
blocks/          ← handler（ビジネスロジックの入口）
domain/          ← ドメインロジック
transport/       ← HTTP / stdio / UDS サーバー
flows/           ← Flow 定義
webapp/          ← Tobkiri standalone frontend の source
ui/              ← 配信される build 済み frontend（shell.html, shell-app.js など）
```

### 3. 環境変数の設定

defaults Pack の HTTP サーバーは以下の環境変数を参照します。`transport/http.py` の `DefaultsHttpServer.__init__` で読み取られます。

| 環境変数 | デフォルト値 | 説明 |
|---|---|---|
| `DEFAULTS_HTTP_HOST` | `127.0.0.1` | HTTP サーバーのバインドアドレス |
| `DEFAULTS_HTTP_PORT` | `8766` | HTTP サーバーのポート番号 |

AI プロバイダーを使用する場合は、各プロバイダーの API キーも設定してください（例: `OPENAI_API_KEY`、`ANTHROPIC_API_KEY` など）。API キーが未設定の場合、AI 呼び出しはスタブ応答（`[stub] AI response placeholder`）を返します。

```bash
export DEFAULTS_HTTP_HOST=127.0.0.1
export DEFAULTS_HTTP_PORT=8766
export OPENAI_API_KEY=sk-...
```

## 起動方法

defaults Pack はカーネルから起動されます。カーネルが `defaults.frontend.start` handler を呼び出すと、`blocks/frontend/start.py` の `run()` が実行されます。`run()` は `input_data` から `facade` を取得し、`transport.http.start_http_server(facade)` を呼んで HTTP サーバーを起動します。`facade` が `None` の場合はエラーを返します。

```python
# blocks/frontend/start.py の動作概要
def run(input_data, context):
    from transport.http import start_http_server
    facade = input_data.get("facade")
    if facade is None:
        return error("facade is required")
    server = start_http_server(facade)
    return ok({
        "message": "HTTP server started",
        "host": server.host,
        "port": server.port,
    })
```

起動が成功すると、コンソールに以下のメッセージが表示されます。

```
[defaults] HTTP server started on 127.0.0.1:8766
```

## フロントエンドを編集するとき

`http://127.0.0.1:8766/` で出る standalone UI の source は `webapp/` にあります。`Tobkiri` として、`defaultspack` の実 API に繋ぐ形で管理しています。

```bash
cd tobkiri_runtime/ecosystem/defaultspack/webapp
npm install
npm run dev
```

本番相当の配信ファイルを更新したいときは build します。

```bash
cd tobkiri_runtime/ecosystem/defaultspack/webapp
npm run build
```

この build は `ui/` に `shell-app.js` と `shell-app.css` を出力します。HTTP サーバーは `ui/shell.html` を返し、そこから `/static/shell-app.js` と `/static/shell-app.css` を読み込みます。

## 最初の会話を送るまでの手順

### ブラウザで開く

ブラウザで `http://127.0.0.1:8766/` にアクセスすると、`ui/shell.html` が返されます。`shell.html` は `webapp` の build 済み asset を mount するだけの薄い入口です。UI が表示されれば起動成功です。

HTTP サーバーのルート `/` は `transport/http.py` の `_handle_static()` が処理し、Pack ルートからの相対パス `ui/shell.html` を読み込んで返します。追加の静的ファイル（CSS、JS、画像等）は `/static/{path}` でアクセスでき、`_handle_static_file()` が `ui/{path}` からファイルを読み込みます。例えば `/static/shell-app.js` は `ui/shell-app.js`、`/static/dev_panel.js` は `ui/dev_panel.js` を返します。

### curl で会話を作成してメッセージを送る

#### 1. 会話を作成する

```bash
curl -X POST http://127.0.0.1:8766/api/chat/conversations \
  -H "Content-Type: application/json" \
  -d '{"model": "stub/default"}'
```

レスポンス（`blocks/chat/create_conversation.py` → `domain/chat/store.py` の `create_conversation()`）:

```json
{
  "status": "ok",
  "data": {
    "id": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
    "title": "New Conversation",
    "model": "stub/default",
    "messages": [],
    "current_node_id": null,
    "tags": [],
    "is_starred": false,
    "is_archived": false,
    "created_at": 1700000000000,
    "updated_at": 1700000000000
  }
}
```

#### 2. メッセージを送信する

返された `id` を `{conversation_id}` として使用します。

```bash
curl -X POST http://127.0.0.1:8766/api/chat/conversations/{conversation_id}/messages \
  -H "Content-Type: application/json" \
  -d '{
    "message": {
      "role": "user",
      "content": "Hello, world!"
    }
  }'
```

このリクエストは `blocks/chat/send.py` の `run()` を呼び出します。ユーザーメッセージを保存し、会話履歴を AI に送信し、AI の応答を assistant メッセージとして保存して返します。

```json
{
  "status": "ok",
  "data": {
    "id": "...",
    "role": "assistant",
    "content": [{"type": "text", "text": "[stub] AI response placeholder"}],
    "conversation_id": "...",
    "parent_id": "...",
    "sequence_number": 2,
    "finish_reason": "stop",
    "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
  }
}
```

## トラブルシューティング

### サーバーが起動しない

- `DEFAULTS_HTTP_PORT` が他のプロセスに使われていないか確認してください。
- `input_data` に `facade` が含まれていない場合、`blocks/frontend/start.py` は `error("facade is required")` を返します。カーネルから正しく facade が渡されているか確認してください。

### `[stub] AI response placeholder` が返される

- AI プロバイダーの API キーが設定されていない場合、または `call_handler` が `None` の場合にスタブ応答が返されます。
- `blocks/chat/send.py` の `_stub_response()` がフォールバックとして使われています。
- 実際の AI 応答を得るには、環境変数で API キーを設定し、会話の `model` に有効なモデル名（例: `openai/gpt-4o`）を指定してください。

### 会話が見つからない（NOT_FOUND）

- `ChatStore` はインメモリのシングルトンです（`domain/chat/store.py`）。サーバーを再起動すると全ての会話データが失われます。
- 会話作成で返された `id` が正しいか確認してください。

### CORS エラー

- HTTP サーバーは全てのオリジンからのアクセスを許可しています（`Access-Control-Allow-Origin: *`）。CORS が問題になる場合は、ブラウザの拡張機能やプロキシの影響を確認してください。

### ヘルスチェック

サーバーの稼働状態は以下で確認できます。

```bash
curl http://127.0.0.1:8766/api/health
```

```json
{
  "status": "ok",
  "data": {
    "status": "healthy",
    "pack": "defaults",
    "ts": "2025-01-01T00:00:00Z"
  }
}
```
