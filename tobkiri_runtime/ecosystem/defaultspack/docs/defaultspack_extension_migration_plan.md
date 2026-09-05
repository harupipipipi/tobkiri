# defaultspack Extension Migration Plan (PR統合版)

## 背景と目的

defaultspack には、以下の集中実装が残っている。

- LLM provider / model の中央定義（`domain/ai_client/providers/__init__.py` と `model_profiles.py`）
- prompt / tool / knowledge / transport の重複管理パス
- `transport/http.py` の巨大な fallback ルート表

本変更では **manifest駆動 + ファイルドロップ拡張** を基盤にし、互換を保ったまま段階移行できる土台を作る。

## 実装方針（このPRで完了させる範囲）

1. 拡張カテゴリを固定文字列で定義し、カテゴリごとの discovery ルールを明示する。
2. manifest 検証と extension registry を追加し、中央ハードコードからの脱却を開始する。
3. LLM provider/model は extension manifest 優先で読み込み、既存ロジックは互換 fallback として残す。
4. OpenRouter は静的一覧を持たず、API同期 + キャッシュ + fallback で扱う。
5. prompt / tool / knowledge / transport は既存 manager を壊さず、extension registry 側を一次ソースに寄せる。
6. 既存 API/呼び出しシグネチャ（`AIClient.complete(model, messages, tools, params)`）は維持する。

## 拡張カテゴリ（foundation）

- `llm_provider`
- `llm_model`
- `prompt`
- `tool`
- `chat_mode`
- `agent_mode`
- `knowledge_backend`
- `transport`
- `ui_surface`
- `policy`

## ディレクトリ規約（foundation）

```text
ecosystem/defaultspack/extensions/
  llm/providers/<provider_id>/manifest.json
  llm/providers/<provider_id>/models/*.json
  prompts/<prompt_id>/manifest.json
  tools/<tool_id>/manifest.json
  chat_modes/<mode_id>/manifest.json
  agent_modes/<mode_id>/manifest.json
  knowledge_backends/<backend_id>/manifest.json
  transports/<transport_id>/manifest.json
  ui/<surface_id>/manifest.json
  policies/<policy_id>/manifest.json
```

## Detailed TODO（受け入れ基準つき）

### A. Foundation

- [x] A1: 作業ブランチを作成
  - 受け入れ: `codex/defaultspack-extension-refactor` で作業する
- [x] A2: defaultspack 主要テストのベースラインを確認
  - 受け入れ: extension 追加後も phase5 テストは維持される
- [x] A3: 本 migration plan を追加
  - 受け入れ: 目的・範囲・カテゴリ・互換方針・TODO が明記される
- [x] A4: Extension discovery / manifest validation / registry 実装
  - 受け入れ: カテゴリ別に manifest を検出し、検証エラーを取得できる
- [ ] A5: legacy import path と canonical package path の二重化を解消
  - 受け入れ: manifest entrypoint 読み込みで `domain.*` と `ecosystem.defaultspack.*` が衝突しない

### B. LLM / Provider 移行（互換維持）

- [ ] B1: `domain.ai_client.providers.__init__` を extension manifest 駆動へ置換
  - 受け入れ: 中央 `_PROVIDER_REGISTRY` 依存が解消される
- [ ] B2: OpenAI互換 generic adapter を追加
  - 受け入れ: manifest の env/base_url 設定だけで provider を追加できる
- [ ] B3: OpenRouter provider を追加（動的モデル同期）
  - 受け入れ: ハードコードモデル一覧なし、`GET /api/v1/models` 同期 + キャッシュ + fallback が動く
- [ ] B4: 既定モデル選択を manifest / model metadata ベースに移行
  - 受け入れ: stale 固定値に依存しない（例: OpenAI は `gpt-5.5`、Anthropic は Claude 4.6 系、Google は Gemini 2.5 系）
- [ ] B5: OpenAI / Anthropic / Google の modern catalog を manifest 側へ寄せる
  - 受け入れ: `ProfileLoader` の default / fast / large / embedding が registry 起点で決まる
- [ ] B6: OpenRouter と generic OpenAI-compatible を分離する
  - 受け入れ: OpenRouter 固有同期ロジックと generic endpoint adapter が別実装になる

### C. Prompt / Tool / Knowledge / Transport 接続

- [ ] C1: prompt registry を PromptManager に接続
  - 受け入れ: extension prompt が list/get/render でき、user_data prompt 編集は継続する
- [ ] C2: tool registry を ToolRegistry に接続
  - 受け入れ: built-in tool は manifest 起点で読み込まれ、dynamic tool CRUD は継続する
- [ ] C3: knowledge backend manifest を backend registry に接続
  - 受け入れ: entrypoint から backend を生成できる
- [ ] C4: chat_mode / agent_mode runner を entrypoint 解決可能にする
  - 受け入れ: mode manifest が runner 呼び出しの起点になる
- [ ] C5: transport/http.py の fallback ルート表を外出しする
  - 受け入れ: ルート定義が transport registry module 側へ寄り、`http.py` は dispatcher 中心になる
- [ ] C6: prompt / tool / chat_mode / agent_mode / knowledge_backend / transport / ui / policy の manifest 雛形を完成させる
  - 受け入れ: discovery 結果に全カテゴリが現れる

### D. テスト

- [ ] D1: manifest validation テスト
  - 受け入れ: 必須項目欠落・カテゴリ不一致を検知
- [ ] D2: extension discovery テスト
  - 受け入れ: 全カテゴリが検出される
- [ ] D3: provider/model loading テスト
  - 受け入れ: manifest 駆動の provider 検出・model 優先解決が動く
- [ ] D4: OpenRouter 同期/キャッシュテスト
  - 受け入れ: API成功時にキャッシュ更新、API失敗時にキャッシュ fallback
- [ ] D5: PromptManager / ToolRegistry extension 接続テスト
  - 受け入れ: extension 由来の prompt/tool が既存 API から見える
- [ ] D6: transport route registration テスト
  - 受け入れ: fallback route 定義が registry module から構築される
- [ ] D7: legacy shim removal テスト
  - 受け入れ: `prompt.prompt_loader` / `tool.tool_loader` の互換 import ができない

## 互換方針

- API surface は維持する（`AIClient` 呼び出しシグネチャは変更しない）。
- extension 未配置時は fail-soft で既存挙動にフォールバックする。
- `transport/http.py` の fallback ルート表は互換用途として残すが、定義そのものは registry module 側へ寄せる。
- top-level `prompt.*` / `tool.*` legacy shims have been removed; use defaultspack registries and functions.

## 前提と仮定

- `ecosystem/defaultspack` を refactor 対象とし、`ecosystem/defaults` はこのPRでは非対象。
- OpenRouter のモデル取得は `/models` エンドポイントを一次ソースとし、ネットワーク不可時はローカルキャッシュを使用する。
- provider 追加は「manifest追加 + 必要なら adapter 指定」で成立するようにする。

## Current Status

- discovery / registry / basic manifests は追加済み
- provider migration は途中で、package import path の正規化と model metadata の寄せ先整理が必要
- prompt / tool / transport は雛形追加済みだが、既存 manager / route table への接続が未完了
- setup pack selection がある環境では、backend/frontend extension discovery は
  `defaultspack` と選択済み target pack に絞られる。selection がない開発環境では
  互換のため全 sibling pack を読み込む
- Copilot 変更には互換 shim 削除が含まれていたため、このPRでは shim を戻して互換優先にする
## Local-first completion status

This PR fixes the local-first runtime baseline without moving Cloudflare,
Supabase, login, account creation, or user management into defaultspack scope.

Completed in this slice:

- canonical implementation is `tobkiri_runtime/ecosystem/defaultspack/`;
- old `defaults.*` compatibility should delegate to defaultspack behavior rather
  than becoming a second source of truth;
- `stub/default` is the guaranteed no-key model default;
- cloud provider auto-registration is opt-in through
  `RUMI_DEFAULTSPACK_ENABLE_CLOUD_PROVIDERS`;
- local providers are treated as no-key providers in backend and frontend
  catalogs;
- sensitive coding HTTP routes pass the local guard;
- write/delete/patch/restore, terminal medium/high-risk execution, git commit,
  and git push require signed one-time approval tokens;
- approval tokens are bound to operation and argument hash;
- local action attempts and outcomes are written to a redacted JSONL audit log;
- frontend model fallback and optional operations-company calls are catalog
  driven;
- `scripts/quality/scan_defaultspack_integrity.py --strict` verifies the v4 Pack,
  contract catalog, artifact index, executable catalog, bundle lock, declared
  implementation hashes, and the local-first/safety source guards. It does not
  use a legacy manifest, Registry, or authority projection.

Remaining extension work should stay manifest-driven and should avoid adding
cloud defaults back into the fresh local runtime.
