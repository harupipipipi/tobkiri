
# defaultspack

## Canonical implementation

For Tobkiri, the canonical defaultspack implementation is
`rumi_ai_1_10/ecosystem/defaultspack/`.

`rumi_ai_1_10/` is an intentional compatibility directory name on this branch;
new public-facing documentation and product references use **Tobkiri**.

The older `ecosystem/defaults/` package and the separate
`harupipipipi/rumiai_defaults` repository are treated as compatibility or
snapshot sources, not as the source of truth for new runtime behavior. New
handler implementations, local safety policy, frontend routes, model defaults,
and quality checks should land in `ecosystem/defaultspack/` first. Legacy
`defaults.*` callers should be served through compatibility aliases or shims
that delegate to defaultspack behavior.

defaultspack is local-first by default:

- A fresh runtime starts with `stub/default`; cloud model providers are opt-in.
- Coding, terminal, and git mutations are protected as local operations, not as
  user-account authentication.
- Sensitive local HTTP mutations require loopback access, local origins, CSRF
  metadata when an Origin is present, signed one-time approval tokens, and
  redacted JSONL audit records.
- Cloudflare, Supabase, login, account creation, and user management are out of
  scope for defaultspack local operation protection.

Tobkiri runtime の標準パックです。

runtime 本体はドメイン知識を持たない汎用カーネルです。defaultspack は ecosystem に「AI サービスとして動作するための全ての仕組み」を提供します。チャット、エージェント、ツール、プロンプト、AI クライアント、コーディング支援、マルチモーダル処理、フロントエンド通信は、defaultspack の handler と domain コードで動作します。

ただし defaults が提供するのは「仕組み」だけである。具体的な UI、ツール定義、エージェント定義、プロンプト、テーマ、レイアウトは全て user_data 側に配置される。defaults はそれらを「置ける場所」と「動かす仕組み」を提供する。

defaults 単体で既存の AI サービス（ChatGPT / Claude / Cursor / Devin）と正面から戦えるレベルの品質を目指す。

---

## 思想

**Batteries Included, But Every Battery Is Removable.** defaults を入れれば全機能が動く。しかし任意のコンポーネントを別パックで置き換えられる。

**Defaults Defines the Standard, Not the Limit.** defaults が定義する権限・handler・ドメインモデルは rumiai ecosystem の「標準語彙」になる。他のパックはこの語彙を使う。しかしこの語彙は拡張可能であり、defaults が知らない概念を他のパックが追加できる。

**Know Everything, Assume Nothing.** defaults は AI サービスに必要なドメイン知識を全て持つ。しかしユーザーの環境、ユースケース、好みについて何も仮定しない。

**Security by Capability, Not by Trust.** defaults は rumiai のセキュリティモデルに完全に従う。defaults 自身も Grant された権限の範囲内でのみ動作する。

**Infrastructure Only, Content in user_data.** defaults が提供するのはドメインロジック（handler）、通信基盤、Widget ライブラリ、シェル、Flow 定義のみ。画面の見た目（Asset）、ツール定義、エージェント設定、プロンプト、テーマ、レイアウトは全て user_data に配置される。defaults はそれらが動くための API とフレームワークを提供する。

---

## defaults が提供するもの

- **handler** — call_handler で呼び出せるドメイン操作 API。chat、agent、coding、ai、tool、prompt、memory、media の各ドメインの基本操作。
- **domain コード** — handler の実装。各ドメインのビジネスロジック。
- **Flow 定義** — simple_chat、agent_chat、planning_agent。デフォルトの処理パイプライン。
- **モデル能力ルーティング** — vision / tool / thinking / speed / knowledge_level を見て、モデルグループ内で実モデルを選ぶ。画像非対応モデルには Vision Bridge で画像文脈を渡す。
- **通信基盤** — frontend handler + transport。HTTP、stdio、UDS 経由の通信。
- **Widget ライブラリ** — lib/rumi_widgets/。バックエンドが UI に描画指示を出すための Python ヘルパー。
- **シェル** — ui/shell.html。スロット定義 + Asset ローダー + Widget レンダラー。Asset を載せる空の枠。
- **RumiTemplate catalog** — `templates/` の JSON から settings、AI input、tool/context policy、commands、backend bindings、test contracts を安全に合成する metadata layer。実行権限は kernel/runtime 側に残る。

## まずどこを見るか

| やりたいこと | 読む場所 |
|---|---|
| docs の入口から探したい | `docs/index.md` |
| PR97 の全体像と UI/chat/tool/MCP/skill/memory/scheduler/trigger の関係を知りたい | `docs/defaultspack-explained.md` |
| AI agent service defaults の全体像を見たい | `docs/ai_agent_services_feature_catalog.md`, `docs/local_agent_implementation_plan.md` |
| ローカル優先・承認・安全方針を見たい | `docs/local_first_policy.md`, `docs/safety_permission_audit_design.md` |
| capability / profile / preset を機械可読に見たい | `/api/agent-service/manifest`, `/api/capabilities`, `capabilities/`, `profiles/`, `presets/` |
| defaultspack を standalone で起動したい | `docs/getting-started.md` |
| 8766 のフロントエンドを直したい | `webapp/` |
| rumi_bundle の metadata を見たい | `docs/rumi_bundle.md` |
| 右バー / 設定 / chat renderer の拡張方法を知りたい | `docs/frontend_extensions.md` |
| RumiTemplate で設定、AI input、tool policy、commands を合成したい | `docs/templates.md` |
| AI Agent Service Defaults の全体像を知りたい | `docs/ai_agent_services_feature_catalog.md`, `docs/local_agent_implementation_plan.md` |
| local-first policy / safety / compact の設計を知りたい | `docs/local_first_policy.md`, `docs/safety_permission_audit_design.md`, `docs/compact_context_design.md` |
| capability/profile/preset を使いたい | `capabilities/`, `profiles/local_agent.profile.yaml`, `presets/local_only_safe.preset.yaml` |
| frontend の次タスクを見たい | `docs/frontend_todo.md` |
| ブラウザに返す実ファイルの置き場を見たい | `ui/` |
| Browser Companion extension を見たい | `browser_extensions/rumi_browser_companion/` |
| HTTP エンドポイントを見たい | `docs/chat.md`, `transport/http.py` |
| viewer 経由の起動フローを知りたい | `../../docs/rumi_viewer_start.md` |

`webapp/` は `rumi DP` の standalone frontend source です。`defaultspack` の `/api/chat/...`、`/api/ui/...`、`/api/health` に接続します。`npm run build` の出力先は `ui/` で、HTTP サーバーはその build 済み asset を `/` と `/static/...` で配信します。

## AI Agent Service Defaults

defaultspack includes local-first building blocks inspired by Codex, Claude Code, ChatGPT Projects, Manus, Genspark, and OpenClaw. The core contract is:

- Core behavior works without API keys.
- File, terminal, git, memory, project, compact, artifact, and safety capabilities are cataloged in `capabilities/*.capability.yaml`.
- API/network/browser/cloud integrations are optional providers and approval-gated.
- `domain/capability/catalog.py` exposes capability metadata to backend blocks and the right sidebar.
- The default local profile is `profiles/local_agent.profile.yaml`.

Start with `docs/local_agent_implementation_plan.md` for the roadmap and `docs/ui_agent_experience_design.md` for the right-sidebar/widget experience.

For install/onboarding parity checks against Genspark, Manus, Cline, Hermes,
and OpenClaw, see `docs/competitive_agent_install_eval.md`.

## defaults が提供しないもの

- **任意実行 UI/Asset** — 画面に描画される独自 UI ファイル。template は builtin renderer へ metadata を渡せるが、任意 React module の実行権限は持たない。
- **ツール定義** — tool.json + handler.py。user_data/shared/tools/ に配置される。
- **エージェント定義** — agent.json。user_data/shared/agents/ に配置される。
- **プロンプト定義** — user_data/shared/prompts/ に配置される。
- **テーマ定義** — theme.yaml。user_data/themes/ に配置される。
- **レイアウト定義** — layout.json。user_data/layouts/ に配置される。
- **AI モデルプロファイル** — user_data/shared/ai_models/ に配置される。

---

## Tool Context API

tool の handler.py に注入される context は汎用プリミティブのみで構成される。特定のドメイン（チャット、エージェント等）に特化した API は存在しない。全てのドメイン操作は汎用プリミティブの組み合わせで実現する。

### 常に注入される（宣言不要）

| context キー | 説明 |
|---|---|
| `call_handler(handler_name, params)` | 任意の handler を呼び出す。Grant で許可された権限の範囲内でのみ実行可能 |
| `emit_event(event_type, data)` | イベントを発行する。handler、Flow トリガー、フロントエンドが受信可能 |
| `wait_event(event_type, timeout, filter)` | イベントを待つ。タイムアウト指定可能 |
| `emit_widget(widget_json)` | Widget JSON を UI に送出する |
| `cancel_check()` | キャンセル確認 |
| `handler_config` | conditions.json から注入された設定 |
| `session` | セッション情報（session_id、workspace 等） |

### capabilities_required で宣言して注入されるもの

| capability_id | context キー | 説明 | リスク |
|---|---|---|---|
| `data_read` | `data_read(path) → str/bytes` | user_data 配下のファイル読み取り | 低 |
| `data_write` | `data_write(path, content)` | user_data 配下のファイル書き込み | 中 |
| `execute_flow` | `execute_flow(flow_id, input) → FlowResult` | Flow を起動する | 中 |
| `shell_exec` | `capability("shell_exec", {...})` | シェルコマンド実行 | 高 |
| `browser_control` | `capability("browser_control", {...})` | ブラウザ操作 | 高 |
| `container_exec` | `capability("container_exec", {...})` | Docker コンテナの起動・操作・破棄 | 高 |
| `app_control` | `capability("app_control", {...})` | ホストアプリ操作 | 高 |
| `http_request` | `capability("http_request", {...})` | 外部 HTTP 通信 | 中 |
| `llm_call` | `capability("llm_call", {...})` | ツール内 LLM 呼び出し | 中 |
| `session_state` | `capability("session_state", {...})` | セッション状態読み書き | 低 |

### call_handler の仕組み

call_handler は defaults や Pack が登録した任意の handler を呼び出す汎用ゲートウェイである。

```python
result = context["call_handler"]("defaults.chat.send", {
    "conversation_id": "conv-1",
    "content": "hello"
})
```

呼び出し元 tool の権限を検証し、呼び出し先 handler が要求する権限が含まれていなければ拒否する。含まれていれば handler を実行し結果を返す。

チャット操作、エージェント起動、メモリ読み書き、プロンプトレンダリング、全てが call_handler 経由で行える。新しい handler が Pack によって追加されれば、tool は同じ call_handler でそれも呼び出せる。

### emit_event / wait_event の仕組み

イベントはシステム全体の汎用通信メカニズムである。

```python
context["emit_event"]("my_tool.done", {"result": "success"})

response = context["wait_event"]("ui.user_response", timeout=30, filter={"id": "popup_1"})
```

フロントエンドへのポップアップ表示、tool 間の非同期通信、Flow トリガーのフック、全てが同じ仕組みで実現される。

### container_exec capability

Docker コンテナのライフサイクルを操作する汎用 capability である。display オプションが true の場合、コンテナ内に仮想フレームバッファが起動し、screenshot と input（click, type, key, scroll）が使用可能になる。

```python
container = context["capability"]("container_exec", {
    "action": "create",
    "image": "ubuntu:22.04",
    "options": {"display": True, "memory_limit": "512m"}
})

context["capability"]("container_exec", {
    "action": "exec",
    "container_id": container["id"],
    "command": "ls -la"
})

context["capability"]("container_exec", {
    "action": "screenshot",
    "container_id": container["id"]
})

context["capability"]("container_exec", {
    "action": "input",
    "container_id": container["id"],
    "input_type": "click",
    "x": 500, "y": 300
})

context["capability"]("container_exec", {
    "action": "destroy",
    "container_id": container["id"]
})
```

---

## 権限カタログ

defaults は rumiai ecosystem の「標準語彙」として権限を定義する。tool、handler、Pack はこれらの権限を Grant で取得して操作を行う。

### 命名規則

`domain.resource.action` のドット区切り3層。ワイルドカード `*` で一括指定可能。

```
chat.conversation.create     → chat ドメイン、conversation リソース、create アクション
chat.conversation.*          → conversation の全アクション
chat.*                       → chat ドメインの全権限
```

### chat ドメイン（18権限）

| 権限 | 説明 |
|------|------|
| `chat.conversation.create` | 会話作成 |
| `chat.conversation.read` | 会話読み取り |
| `chat.conversation.list` | 会話一覧 |
| `chat.conversation.update` | 会話更新 |
| `chat.conversation.delete` | 会話削除 |
| `chat.conversation.export` | 会話エクスポート |
| `chat.conversation.branch` | 会話分岐 |
| `chat.message.send` | メッセージ送信 |
| `chat.message.read` | メッセージ読み取り |
| `chat.message.edit` | メッセージ編集 |
| `chat.message.delete` | メッセージ削除 |
| `chat.message.regenerate` | AI 応答再生成 |
| `chat.message.stream` | ストリーミング |
| `chat.message.stop` | ストリーミング停止 |
| `chat.attachment.upload` | 添付アップロード |
| `chat.attachment.read` | 添付読み取り |
| `chat.reaction.write` | リアクション |
| `chat.search` | メッセージ検索 |

### agent ドメイン（18権限）

| 権限 | 説明 |
|------|------|
| `agent.create` | エージェント作成 |
| `agent.read` | エージェント読み取り |
| `agent.list` | エージェント一覧 |
| `agent.update` | エージェント更新 |
| `agent.delete` | エージェント削除 |
| `agent.execute` | エージェント実行 |
| `agent.step.read` | ステップ読み取り |
| `agent.step.approve` | ステップ承認 |
| `agent.step.reject` | ステップ拒否 |
| `agent.cancel` | 実行キャンセル |
| `agent.pause` | 一時停止 |
| `agent.resume` | 再開 |
| `agent.status.read` | 状態読み取り |
| `agent.sub.spawn` | サブエージェント起動 |
| `agent.sub.manage` | サブエージェント管理 |
| `agent.plan.read` | プラン読み取り |
| `agent.plan.modify` | プラン変更 |
| `agent.history.read` | 履歴読み取り |

### tool ドメイン（13権限）

| 権限 | 説明 |
|------|------|
| `tool.invoke` | ツール実行 |
| `tool.read` | ツール読み取り |
| `tool.list` | ツール一覧 |
| `tool.schema.read` | スキーマ読み取り |
| `tool.create` | ツール作成 |
| `tool.update` | ツール更新 |
| `tool.delete` | ツール削除 |
| `tool.result.read` | 実行結果読み取り |
| `tool.permission.read` | 権限読み取り |
| `tool.permission.write` | 権限書き込み |
| `tool.mcp.connect` | MCP サーバー接続 |
| `tool.mcp.disconnect` | MCP サーバー切断 |
| `tool.mcp.list` | MCP ツール一覧 |

### prompt ドメイン（12権限）

| 権限 | 説明 |
|------|------|
| `prompt.create` | プロンプト作成 |
| `prompt.read` | プロンプト読み取り |
| `prompt.list` | プロンプト一覧 |
| `prompt.update` | プロンプト更新 |
| `prompt.delete` | プロンプト削除 |
| `prompt.render` | プロンプトレンダリング |
| `prompt.variable.read` | 変数読み取り |
| `prompt.variable.write` | 変数書き込み |
| `prompt.system.read` | システムプロンプト読み取り |
| `prompt.system.write` | システムプロンプト書き込み |
| `prompt.import` | インポート |
| `prompt.export` | エクスポート |

### ai ドメイン（19権限）

| 権限 | 説明 |
|------|------|
| `ai.completion` | テキスト生成 |
| `ai.stream` | ストリーミング生成 |
| `ai.model.list` | モデル一覧 |
| `ai.model.read` | モデル情報読み取り |
| `ai.provider.list` | プロバイダ一覧 |
| `ai.provider.add` | プロバイダ追加 |
| `ai.provider.remove` | プロバイダ削除 |
| `ai.provider.config.read` | プロバイダ設定読み取り |
| `ai.provider.config.write` | プロバイダ設定書き込み |
| `ai.profile.read` | AI プロファイル読み取り |
| `ai.profile.write` | AI プロファイル書き込み |
| `ai.profile.list` | プロファイル一覧 |
| `ai.usage.read` | 使用量読み取り |
| `ai.token.count` | トークンカウント |
| `ai.embedding` | 埋め込みベクトル生成 |
| `ai.image.generate` | 画像生成 |
| `ai.image.analyze` | 画像解析 |
| `ai.audio.transcribe` | 音声文字起こし |
| `ai.audio.synthesize` | 音声合成 |

### file ドメイン（18権限）

| 権限 | 説明 |
|------|------|
| `file.read` | ファイル読み取り |
| `file.write` | ファイル書き込み |
| `file.create` | ファイル作成 |
| `file.delete` | ファイル削除 |
| `file.move` | ファイル移動 |
| `file.copy` | ファイルコピー |
| `file.list` | ファイル一覧 |
| `file.search` | ファイル検索 |
| `file.watch` | ファイル監視 |
| `file.metadata.read` | メタデータ読み取り |
| `file.permission.read` | 権限読み取り |
| `file.workspace.read` | ワークスペース読み取り |
| `file.workspace.write` | ワークスペース書き込み |
| `file.system.read` | システムファイル読み取り |
| `file.system.write` | システムファイル書き込み |
| `file.temp.write` | 一時ファイル書き込み |
| `file.archive.read` | アーカイブ読み取り |
| `file.archive.create` | アーカイブ作成 |

### terminal ドメイン（11権限）

| 権限 | 説明 |
|------|------|
| `terminal.execute` | コマンド実行 |
| `terminal.read` | 出力読み取り |
| `terminal.stream` | ストリーミング出力 |
| `terminal.session.create` | セッション作成 |
| `terminal.session.list` | セッション一覧 |
| `terminal.session.close` | セッション終了 |
| `terminal.interrupt` | 割り込み |
| `terminal.env.read` | 環境変数読み取り |
| `terminal.env.write` | 環境変数書き込み |
| `terminal.cwd.read` | カレントディレクトリ読み取り |
| `terminal.cwd.write` | カレントディレクトリ変更 |

### git ドメイン（15権限）

| 権限 | 説明 |
|------|------|
| `git.status` | ステータス確認 |
| `git.diff` | 差分表示 |
| `git.log` | ログ表示 |
| `git.commit` | コミット |
| `git.branch.list` | ブランチ一覧 |
| `git.branch.create` | ブランチ作成 |
| `git.branch.switch` | ブランチ切り替え |
| `git.branch.delete` | ブランチ削除 |
| `git.merge` | マージ |
| `git.push` | プッシュ |
| `git.pull` | プル |
| `git.stash` | スタッシュ |
| `git.reset` | リセット |
| `git.remote.list` | リモート一覧 |
| `git.remote.manage` | リモート管理 |

### memory ドメイン（13権限）

| 権限 | 説明 |
|------|------|
| `memory.short.read` | 短期メモリ読み取り |
| `memory.short.write` | 短期メモリ書き込み |
| `memory.long.read` | 長期メモリ読み取り |
| `memory.long.write` | 長期メモリ書き込み |
| `memory.long.delete` | 長期メモリ削除 |
| `memory.long.search` | 長期メモリ検索 |
| `memory.project.read` | プロジェクトメモリ読み取り |
| `memory.project.write` | プロジェクトメモリ書き込み |
| `memory.user.read` | ユーザーメモリ読み取り |
| `memory.user.write` | ユーザーメモリ書き込み |
| `memory.vector.store` | ベクトル保存 |
| `memory.vector.query` | ベクトル検索 |
| `memory.clear` | メモリクリア |

### media ドメイン（12権限）

| 権限 | 説明 |
|------|------|
| `media.image.read` | 画像読み取り |
| `media.image.create` | 画像作成 |
| `media.image.transform` | 画像変換 |
| `media.audio.read` | 音声読み取り |
| `media.audio.create` | 音声作成 |
| `media.audio.transcribe` | 音声文字起こし |
| `media.video.read` | 動画読み取り |
| `media.document.read` | ドキュメント読み取り |
| `media.document.parse` | ドキュメント解析 |
| `media.clipboard.read` | クリップボード読み取り |
| `media.clipboard.write` | クリップボード書き込み |
| `media.screenshot` | スクリーンショット |

### flow ドメイン（12権限）

| 権限 | 説明 |
|------|------|
| `flow.execute` | Flow 実行 |
| `flow.read` | Flow 読み取り |
| `flow.list` | Flow 一覧 |
| `flow.create` | Flow 作成 |
| `flow.update` | Flow 更新 |
| `flow.delete` | Flow 削除 |
| `flow.status.read` | 実行状態読み取り |
| `flow.cancel` | 実行中 Flow キャンセル |
| `flow.modifier.apply` | Flow Modifier 適用 |
| `flow.modifier.list` | Modifier 一覧 |
| `flow.context.read` | Flow コンテキスト読み取り |
| `flow.context.write` | Flow コンテキスト書き込み |

### config ドメイン（13権限）

| 権限 | 説明 |
|------|------|
| `config.read` | 設定読み取り |
| `config.write` | 設定書き込み |
| `config.profile.read` | プロファイル読み取り |
| `config.profile.write` | プロファイル書き込み |
| `config.profile.list` | プロファイル一覧 |
| `config.theme.read` | テーマ読み取り |
| `config.theme.write` | テーマ書き込み |
| `config.keybind.read` | キーバインド読み取り |
| `config.keybind.write` | キーバインド書き込み |
| `config.locale.read` | ロケール読み取り |
| `config.locale.write` | ロケール書き込み |
| `config.export` | 設定エクスポート |
| `config.import` | 設定インポート |

### net ドメイン（11権限）

| 権限 | 説明 |
|------|------|
| `net.http.request` | HTTP リクエスト |
| `net.http.stream` | HTTP ストリーミング |
| `net.websocket.connect` | WebSocket 接続 |
| `net.websocket.send` | WebSocket 送信 |
| `net.dns.resolve` | DNS 解決 |
| `net.proxy.read` | プロキシ読み取り |
| `net.proxy.write` | プロキシ書き込み |
| `net.allowlist.read` | 許可リスト読み取り |
| `net.allowlist.write` | 許可リスト書き込み |
| `net.download` | ダウンロード |
| `net.upload` | アップロード |

### frontend ドメイン（12権限）

| 権限 | 説明 |
|------|------|
| `frontend.render.mount` | Asset を描画面に載せる |
| `frontend.render.unmount` | 描画面から外す |
| `frontend.render.update` | 描画内容を更新する |
| `frontend.message.send` | バックエンド → 描画面 |
| `frontend.message.receive` | 描画面 → バックエンド |
| `frontend.message.stream` | 連続的にデータを流す |
| `frontend.asset.register` | Asset の登録を受け入れる |
| `frontend.asset.unregister` | Asset の解除 |
| `frontend.asset.list` | 登録 Asset の一覧 |
| `frontend.layout.read` | レイアウト情報取得 |
| `frontend.layout.write` | レイアウト変更・保存 |
| `frontend.theme.read` | テーマ情報取得 |

### event ドメイン（5権限）

| 権限 | 説明 |
|------|------|
| `event.emit` | イベント発行 |
| `event.subscribe` | イベント購読 |
| `event.unsubscribe` | イベント購読解除 |
| `event.list` | イベント一覧 |
| `event.history.read` | イベント履歴読み取り |

### audit ドメイン（3権限）

| 権限 | 説明 |
|------|------|
| `audit.read` | 監査ログ読み取り |
| `audit.search` | 監査ログ検索 |
| `audit.export` | 監査ログエクスポート |

### pack ドメイン（8権限）

| 権限 | 説明 |
|------|------|
| `pack.list` | パック一覧 |
| `pack.read` | パック読み取り |
| `pack.install` | パックインストール |
| `pack.remove` | パック削除 |
| `pack.update` | パック更新 |
| `pack.approve` | パック承認 |
| `pack.config.read` | パック設定読み取り |
| `pack.config.write` | パック設定書き込み |

### secret ドメイン（4権限）

| 権限 | 説明 |
|------|------|
| `secret.read` | シークレット読み取り |
| `secret.write` | シークレット書き込み |
| `secret.delete` | シークレット削除 |
| `secret.list` | シークレット一覧 |

### kernel ドメイン（5権限）

| 権限 | 説明 |
|------|------|
| `kernel.status.read` | カーネル状態読み取り |
| `kernel.shutdown` | シャットダウン |
| `kernel.restart` | 再起動 |
| `kernel.health` | ヘルスチェック |
| `kernel.version` | バージョン情報 |

### schedule ドメイン（5権限）

| 権限 | 説明 |
|------|------|
| `schedule.create` | スケジュール作成 |
| `schedule.read` | スケジュール読み取り |
| `schedule.update` | スケジュール更新 |
| `schedule.delete` | スケジュール削除 |
| `schedule.list` | スケジュール一覧 |

---

## 権限プリセット

| プリセット | 含む権限 | 用途 |
|-----------|---------|------|
| `preset.chat_basic` | `chat.conversation.*`, `chat.message.*`, `ai.completion`, `ai.stream` | 基本チャット |
| `preset.chat_full` | `preset.chat_basic` + `chat.search`, `chat.attachment.*`, `prompt.*`, `memory.short.*` | フルチャット |
| `preset.coding` | `file.workspace.*`, `terminal.*`, `git.*`, `ai.completion`, `ai.stream` | コーディング |
| `preset.agent_basic` | `agent.*`, `tool.invoke`, `tool.list`, `tool.schema.read`, `ai.*` | 基本エージェント |
| `preset.agent_full` | `preset.agent_basic` + `file.*`, `terminal.*`, `net.*`, `memory.*` | フルエージェント |
| `preset.frontend` | `frontend.*`, `event.*`, `config.read`, `config.theme.*` | フロントエンド |
| `preset.readonly` | `*.read`, `*.list` | 読み取り専用 |
| `preset.admin` | `*`（全権限） | 管理者 |

---

## defaults 自身の権限

defaults は以下の権限で動作する。

```yaml
grants:
  - preset.chat_full
  - preset.agent_full
  - preset.coding
  - preset.frontend
  - memory.*
  - media.*
  - flow.*
  - config.*
  - event.*
  - schedule.*
  - audit.read
  - pack.list
  - pack.read
  - kernel.status.read
  - kernel.health
  - kernel.version
```

以下は defaults に付与されない。rumiai CLI またはユーザーの明示的操作が必要。

`secret.write`, `secret.delete`, `kernel.shutdown`, `kernel.restart`, `pack.install`, `pack.remove`, `pack.approve`

---

## Handler 体系

handler は rumiai の Trust（SHA-256 ハッシュ検証）で承認される。defaults の handler は rumiai ecosystem 上の全ての Pack・Flow・tool が call_handler で呼び出せる標準 API として機能する。

### handler 命名規則

`pack_id.category.name`

```
defaults.frontend.start        → defaults パック、frontend カテゴリ、start handler
defaults.coding.file_read      → defaults パック、coding カテゴリ、file_read handler
some_pack.custom.my_handler    → 別パックの handler
```

### defaults handler 一覧

#### frontend（3 handler）

| handler | 必要な権限 | 説明 |
|---|---|---|
| `defaults.frontend.start` | `frontend.serve`, `frontend.bind`, `frontend.auth.manage` | transport（http/stdio/uds）を起動 |
| `defaults.frontend.stop` | `frontend.serve` | transport を停止 |
| `defaults.frontend.emit` | `frontend.event.emit` | フロントエンドにイベントを送信 |

#### chat（16 handler）

| handler | 必要な権限 | 説明 |
|---|---|---|
| `defaults.chat.create_conversation` | `chat.conversation.create` | 会話作成 |
| `defaults.chat.get_conversation` | `chat.conversation.read` | 会話データ取得 |
| `defaults.chat.list_conversations` | `chat.conversation.list` | 会話一覧 |
| `defaults.chat.update_conversation` | `chat.conversation.update` | 会話メタデータ更新 |
| `defaults.chat.delete_conversation` | `chat.conversation.delete` | 会話削除 |
| `defaults.chat.export_conversation` | `chat.conversation.export` | 会話エクスポート |
| `defaults.chat.send` | `chat.message.send`, `ai.completion` | メッセージ送信 + AI 応答生成 |
| `defaults.chat.stream` | `chat.message.stream`, `ai.stream` | ストリーミング応答 |
| `defaults.chat.add_message` | `chat.message.send` | メッセージ追加（AI 応答なし） |
| `defaults.chat.get_message` | `chat.message.read` | メッセージ取得 |
| `defaults.chat.update_message` | `chat.message.edit` | メッセージ編集 |
| `defaults.chat.delete_message` | `chat.message.delete` | メッセージ削除 |
| `defaults.chat.branch` | `chat.conversation.branch` | 会話分岐 |
| `defaults.chat.search` | `chat.search` | メッセージ検索 |
| `defaults.chat.stop` | `chat.message.stop` | ストリーミング停止 |
| `defaults.chat.regenerate` | `chat.message.regenerate`, `ai.completion` | 応答再生成 |

#### agent（6 handler）

| handler | 必要な権限 | 説明 |
|---|---|---|
| `defaults.agent.execute` | `agent.execute`, `tool.invoke` | エージェント実行 |
| `defaults.agent.approve` | `agent.step.approve` | ステップ承認 |
| `defaults.agent.reject` | `agent.step.reject` | ステップ拒否 |
| `defaults.agent.cancel` | `agent.cancel` | 実行キャンセル |
| `defaults.agent.status` | `agent.status.read` | 状態取得 |
| `defaults.agent.plan` | `agent.plan.read` | プラン取得 |

#### coding（12 handler）

| handler | 必要な権限 | 説明 |
|---|---|---|
| `defaults.coding.file_read` | `file.workspace.read` | ファイル読み取り |
| `defaults.coding.file_write` | `file.workspace.write` | ファイル書き込み |
| `defaults.coding.file_create` | `file.create` | ファイル作成 |
| `defaults.coding.file_delete` | `file.delete` | ファイル削除 |
| `defaults.coding.file_search` | `file.search` | ファイル検索 |
| `defaults.coding.file_list` | `file.list` | ファイル一覧 |
| `defaults.coding.terminal_exec` | `terminal.execute` | コマンド実行 |
| `defaults.coding.terminal_stream` | `terminal.stream` | ストリーミング出力 |
| `defaults.coding.git_status` | `git.status` | Git ステータス |
| `defaults.coding.git_diff` | `git.diff` | Git 差分 |
| `defaults.coding.git_commit` | `git.commit` | Git コミット |
| `defaults.coding.git_push` | `git.push` | Git プッシュ |

#### ai（9 handler）

| handler | 必要な権限 | 説明 |
|---|---|---|
| `defaults.ai.complete` | `ai.completion` | テキスト生成 |
| `defaults.ai.stream` | `ai.stream` | ストリーミング生成 |
| `defaults.ai.models` | `ai.model.list` | モデル一覧 |
| `defaults.ai.providers` | `ai.provider.list` | プロバイダ一覧 |
| `defaults.ai.embed` | `ai.embedding` | 埋め込みベクトル生成 |
| `defaults.ai.image_gen` | `ai.image.generate` | 画像生成 |
| `defaults.ai.image_analyze` | `ai.image.analyze` | 画像解析 |
| `defaults.ai.transcribe` | `ai.audio.transcribe` | 音声文字起こし |
| `defaults.ai.tts` | `ai.audio.synthesize` | 音声合成 |

#### tool（5 handler）

| handler | 必要な権限 | 説明 |
|---|---|---|
| `defaults.tool.invoke` | `tool.invoke` | ツール実行 |
| `defaults.tool.list` | `tool.list` | ツール一覧 |
| `defaults.tool.schema` | `tool.schema.read` | スキーマ読み取り |
| `defaults.tool.mcp_connect` | `tool.mcp.connect` | MCP サーバー接続 |
| `defaults.tool.mcp_list` | `tool.mcp.list` | MCP ツール一覧 |

#### prompt（4 handler）

| handler | 必要な権限 | 説明 |
|---|---|---|
| `defaults.prompt.render` | `prompt.render` | プロンプトレンダリング |
| `defaults.prompt.list` | `prompt.list` | プロンプト一覧 |
| `defaults.prompt.create` | `prompt.create` | プロンプト作成 |
| `defaults.prompt.system` | `prompt.system.read`, `prompt.system.write` | システムプロンプト管理 |

#### memory（5 handler）

| handler | 必要な権限 | 説明 |
|---|---|---|
| `defaults.memory.store` | `memory.long.write` | 長期メモリ保存 |
| `defaults.memory.recall` | `memory.long.read`, `memory.long.search` | 長期メモリ検索・読み取り |
| `defaults.memory.project_context` | `memory.project.read` | プロジェクトメモリ読み取り |
| `defaults.memory.vector_store` | `memory.vector.store` | ベクトル保存 |
| `defaults.memory.vector_query` | `memory.vector.query` | ベクトル検索 |

#### media（6 handler）

| handler | 必要な権限 | 説明 |
|---|---|---|
| `defaults.media.image_read` | `media.image.read` | 画像読み取り |
| `defaults.media.image_transform` | `media.image.transform` | 画像変換 |
| `defaults.media.doc_parse` | `media.document.parse` | ドキュメント解析 |
| `defaults.media.clipboard_read` | `media.clipboard.read` | クリップボード読み取り |
| `defaults.media.clipboard_write` | `media.clipboard.write` | クリップボード書き込み |
| `defaults.media.screenshot` | `media.screenshot` | スクリーンショット |

### 別 Pack が handler を使う例

```yaml
# rumiai-cursor の Flow 定義
# defaults の handler を call_handler で呼ぶだけ

phases:
  - id: boot
    steps:
      - id: start_frontend
        type: handler
        handler: defaults.frontend.start
        params:
          transport: "http"
          port: 0

  - id: main_loop
    steps:
      - id: on_code_request
        type: handler
        handler: defaults.coding.file_read

      - id: custom_sidebar
        type: handler
        handler: cursor.sidebar.render      # Pack 独自の handler

# この Pack の Grant
grants:
  - preset.coding
  - preset.frontend
  - cursor.sidebar.render
```

---

## ファイル構成

```
ecosystem/defaults/
├── README.md                          # 本ドキュメント
├── handlers/
│     └── frontend.py                  # 通信ブリッジ（transport 起動・メッセージ中継）
├── ui/
│     └── shell.html                   # 空の枠 + スロット定義 + Asset ローダー + Widget レンダラー
├── lib/
│     └── rumi_widgets/                # Widget Python ヘルパーライブラリ
│           ├── __init__.py
│           ├── display.py             # Text, CodeBlock, Image, etc.
│           ├── controls.py            # Input, Button, Select, etc.
│           ├── layout.py              # Container, Row, Column, etc.
│           ├── stream.py              # Stream, Indicator
│           └── custom.py              # Custom widget
├── domain/                            # ドメインロジック（handler の実装）
│     ├── chat/                        # chat handler の実装
│     ├── agent/                       # agent handler の実装
│     ├── tool/                        # tool handler の実装
│     ├── prompt/                      # prompt handler の実装
│     ├── ai_client/                   # ai handler の実装
│     ├── coding/                      # coding handler の実装
│     ├── memory/                      # memory handler の実装
│     └── media/                       # media handler の実装
├── flows/                             # デフォルト Flow 定義
│     ├── simple_chat/
│     │     ├── flow.yaml
│     │     └── handler.py
│     ├── agent_chat/
│     │     ├── flow.yaml
│     │     └── handler.py
│     └── planning_agent/
│           ├── flow.yaml
│           └── handler.py
├── transport/                         # 通信トランスポート
│     ├── http.py
│     ├── stdio.py
│     └── uds.py
├── bridge/                            # context 変換・ブリッジ
└── docs/                              # 設計ドキュメント
      ├── frontend.md
      ├── agent.md
      ├── ai_client.md
      ├── chat.md
      ├── flow.md
      ├── prompt.md
      ├── tool.md
      ├── widget.md
      ├── theme.md
      ├── architecture_defaults.md
      ├── profiles_and_models.md
      ├── conflict_resolution.md
      ├── ui_and_layout.md
      └── capability/
            └── dependency-resolution.md
```

user_data 側（defaults がセットアップ時に配置するデフォルトコンテンツ）:

```
user_data/
├── shared/
│     ├── tools/                       # デフォルトツール群
│     ├── agents/                      # デフォルトエージェント定義
│     ├── prompts/                     # デフォルトプロンプト
│     └── ai_models/                   # AI モデルプロファイル
├── assets/                            # デフォルト Asset（chat 画面、agent 画面等）
├── themes/                            # デフォルトテーマ
├── layouts/                           # デフォルトレイアウト
├── chat/                              # 会話データ
├── memory/                            # ユーザーメモリ
└── config.json                        # ユーザー設定
```

---

## ドキュメント一覧

| ファイル | サイズ | 内容 |
|---------|--------|------|
| `docs/index.md` | - | defaultspack docs 入口 |
| `docs/defaultspack-explained.md` | - | PR97 向け全体像と主要フロー図 |
| `docs/architecture_defaults.md` | 3.9KB | defaults 全体アーキテクチャ |
| `docs/agent.md` | 41KB | エージェント設計 |
| `docs/ai_client.md` | 53KB | AI クライアント設計 |
| `docs/chat.md` | 43KB | チャットモジュール設計 |
| `docs/flow.md` | 36KB | Flow Engine 設計 |
| `docs/prompt.md` | 32KB | プロンプト設計 |
| `docs/tool.md` | 35KB | ツールモジュール設計 |
| `docs/frontend.md` | - | フロントエンド設計（改訂予定） |
| `docs/widget.md` | - | Widget 仕様（新規作成予定） |
| `docs/theme.md` | - | テーマ仕様（新規作成予定） |
| `docs/profiles_and_models.md` | 3.2KB | AI モデルプロファイル |
| `docs/conflict_resolution.md` | 3.4KB | 衝突解決 |
| `docs/ui_and_layout.md` | 4.2KB | UI とレイアウト |
| `docs/capability/dependency-resolution.md` | 9.2KB | capability 依存解決 |

---

## 品質目標

defaults 単体で以下と同等以上のユーザー体験を提供する:

- **ChatGPT / Claude** — チャット、マルチモーダル、メモリ
- **Claude Code / Devin** — エージェント、自律コーディング、プランニング
- **Cursor / Windsurf** — コーディング支援、Git 統合、ファイル操作
- **MCP** — 外部ツール連携、プロトコル対応
- **VS Code Extension** — defaults の handler を呼び出す Pack で実現可能

これらは全て defaultspack の handler + user_data のコンテンツ（Asset、tool、agent、prompt）の組み合わせで実現される。
