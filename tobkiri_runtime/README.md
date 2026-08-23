# Rumi AI OS

**「基盤のない基盤」** — 改造される「本体」が存在しないモジュラーAIフレームワーク

---

## 目的別ガイド

コードを全部追わなくても入口が分かるように、用途別の読み先を先に置きます。

| やりたいこと | まず読む場所 | どこまで分かるか |
|---|---|---|
| 目的別にドキュメントを辿りたい | [`docs/README.md`](./docs/README.md) | 「何をしたいか→どのドキュメントか」を一枚で辿れる |
| 用語の意味を揃えたい | [`docs/terminology.md`](./docs/terminology.md) | `rule`, `skill`, `team workspace`, `delegation` の使い分けを確認できる |
| まず起動したい | ルートの [`README.md`](../README.md) | 最短の起動コマンドと repo の入口 |
| まず手を動かして確認したい | [`docs/tutorials/runtime-quickstart.md`](./docs/tutorials/runtime-quickstart.md) | `--health` から `/panel/` までの最短チュートリアル |
| コードを読まずに runtime の仕組みを理解したい | [`docs/concepts/system-mechanism.md`](./docs/concepts/system-mechanism.md) | 起動・Flow・承認・Grant・viewer 連携の実行経路 |
| `tobkiri_launcher` の起動手順と詰まり方を見たい | [`docs/tobkiri_launcher_start.md`](./docs/tobkiri_launcher_start.md) | `401`, 黒画面, panel と defaultspack の関係 |
| defaultspack の frontend を拡張したい | [`ecosystem/defaultspack/docs/frontend_extensions.md`](./ecosystem/defaultspack/docs/frontend_extensions.md) | 右バー、設定、chat renderer、preview feed の増やし方 |
| この runtime の思想を知りたい | この README の `思想` | Flow 中心、Pack 前提、Fail-Soft の考え方 |
| ディレクトリの役割を知りたい | この README の `プロジェクト構造` | `core_runtime/`, `ecosystem/`, `user_data/` の役割 |
| Pack を作る・直す | [`docs/pack-development.md`](./docs/pack-development.md) | `ecosystem.json`, `routes.json`, `permissions.json`, secrets 利用 |
| defaultspack の chat / ai を追いたい | [`ecosystem/defaultspack/README.md`](./ecosystem/defaultspack/README.md) | defaultspack の実装面 |
| defaultspack frontend の今後の作業を見たい | [`ecosystem/defaultspack/docs/frontend_todo.md`](./ecosystem/defaultspack/docs/frontend_todo.md) | registry 化の進捗と次の作業 |
| API キーや secrets を設定したい | [`docs/operations.md`](./docs/operations.md) の Secrets 節 | `user_data/secrets/` と API 経路 |
| viewer 経由の起動経路を直したい | [`../tobkiri_launcher/src-tauri/src/config.rs`](../tobkiri_launcher/src-tauri/src/config.rs) と [`../tobkiri_launcher/src-tauri/src/kernel_manager.rs`](../tobkiri_launcher/src-tauri/src/kernel_manager.rs) | viewer がどの kernel を起動し、どの env を渡すか |
| setup pack / 承認まわりを見たい | [`core_runtime/setup_pack.py`](./core_runtime/setup_pack.py) と [`core_runtime/approval_manager.py`](./core_runtime/approval_manager.py) | setup pack 選択、all-ok grant、再承認 |
| 運用・監査を知りたい | [`docs/operations.md`](./docs/operations.md) と [`docs/roadmap.md`](./docs/roadmap.md) | 運用 API、secrets、今後の方針 |

## 最短の見取り図

1. `app.py` が kernel を起動する
2. `core_runtime/` が Flow, Pack, 承認, 実行基盤を持つ
3. `ecosystem/<pack_id>/` が機能本体を提供する
4. `user_data/` が承認状態, secrets, stores, audit を持つ
5. `tobkiri_launcher/` は kernel を起動して panel に接続する shell になる

## よく使う入口

### 起動確認

```bash
python -m rumi_ai --health
python -m rumi_ai
```

### viewer 開発起動

```bash
cd ../tobkiri_launcher/src-tauri
cargo tauri dev
```

### 代表的なテスト

```bash
python -m pytest tests/test_defaultspack_google_provider.py
python -m pytest tests/test_defaultspack_modules.py
```

---

## 思想

### 贔屓なし（No Favoritism）

Rumi AI の公式コードは「チャット」「ツール」「プロンプト」「AIクライアント」「フロントエンド」といったドメイン概念を**一切知りません**。これらは全て ecosystem 内の Pack が定義します。公式が提供するのは**実行の仕組み**だけです。

### 基盤のない基盤

Minecraft の mod は「Minecraft」という基盤を改造します。しかし Rumi AI には改造される「本体」がありません。全てのアプリケーション機能は Pack として実装され、Flow で結線されます。

### Flow 中心アーキテクチャ

Pack 間の結線・順序・後付け注入を Flow で定義します。既存 Pack の改造なしに新機能を追加できます。

```
          +---------------------------+
          |       Flow Definition     |
          +---------------------------+
                      |
          +---------------------------+
          |    python_file_call       |
          +---------------------------+
            /         |         \
    +--------+  +--------+  +--------+
    | Pack A |  | Pack B |  | Pack C |
    +--------+  +--------+  +--------+
            \         |         /
          +---------------------------+
          |         Kernel            |
          +---------------------------+
```

> **Flow の読み込み元**: `flows/`, `user_data/shared/flows/`, `ecosystem/<pack_id>/backend/flows/`

### Fail-Soft

エラーが発生してもシステムは停止しません。失敗したコンポーネントは無効化され、診断情報に記録されて継続します。

### 悪意 Pack 前提のセキュリティ

ecosystem は第三者が作成でき、悪意ある作者も存在しうるという前提で設計されています。

- **承認必須**: 未承認 Pack のコードは一切実行されない
- **ハッシュ検証**: 承認後にファイルが変更されると自動無効化（再承認必要）
- **Docker 隔離**: 承認済み Pack はコンテナ内で実行（strict モード）
- **Egress Proxy**: 外部通信は UDS ソケット経由のプロキシでのみ許可
- **Capability（Trust + Grant）**: ホスト権限は二段階の承認で制御

既存環境で HMAC 署名なしの設定ファイルを再署名する場合:

```bash
python -m rumi_ai migrate-hmac
```

---

## プロジェクト構造

<details>
<summary>ディレクトリツリー（クリックで展開）</summary>

<pre><code>
project_root/
├── app.py
├── bootstrap.py
├── requirements.txt
├── requirements-dev.txt
│
├── flows/
│   └── 00_startup.flow.yaml
│
├── core_runtime/
│   ├── kernel.py
│   ├── kernel_core.py
│   ├── kernel_handlers_system.py
│   ├── kernel_handlers_runtime.py
│   ├── paths.py
│   ├── diagnostics.py
│   ├── interface_registry.py
│   ├── event_bus.py
│   ├── audit_logger.py
│   ├── install_journal.py
│   ├── approval_manager.py
│   ├── network_grant_manager.py
│   ├── egress_proxy.py
│   ├── rumi_syscall.py
│   ├── syscall.py
│   ├── capability_proxy.py
│   ├── capability_executor.py
│   ├── capability_trust_store.py
│   ├── capability_grant_manager.py
│   ├── capability_installer.py
│   ├── rumi_capability.py
│   ├── python_file_executor.py
│   ├── secure_executor.py
│   ├── container_orchestrator.py
│   ├── component_lifecycle.py
│   ├── host_privilege_manager.py
│   ├── pack_api_server.py
│   ├── flow_loader.py
│   ├── flow_modifier.py
│   ├── flow_composer.py
│   ├── flow_scheduler.py
│   ├── function_alias.py
│   ├── vocab_registry.py
│   ├── shared_dict/
│   │   ├── snapshot.py
│   │   ├── journal.py
│   │   └── resolver.py
│   ├── core_pack/
│   │   ├── core_store_capability/
│   │   ├── core_secrets_capability/
│   │   ├── core_flow_capability/
│   │   ├── core_communication_capability/
│   │   └── core_docker_capability/
│   ├── function_registry.py
│   ├── crypto_utils.py
│   ├── lib_executor.py
│   ├── pip_installer.py
│   ├── pack_importer.py
│   ├── pack_applier.py
│   ├── secrets_store.py
│   ├── store_registry.py
│   ├── unit_registry.py
│   ├── unit_executor.py
│   ├── unit_trust_store.py
│   ├── hierarchical_grant.py
│   ├── lang.py
│   └── permission_manager.py
│
├── backend_core/
│   └── ecosystem/
│       ├── compat.py
│       ├── mounts.py
│       ├── registry.py
│       ├── active_ecosystem.py
│       ├── initializer.py
│       ├── uuid_utils.py
│       └── json_patch.py
│
├── ecosystem/
│   ├── <pack_id>/
│   │   └── backend/
│   │       ├── ecosystem.json
│   │       ├── permissions.json
│   │       ├── requirements.lock
│   │       ├── routes.json
│   │       ├── blocks/
│   │       ├── flows/
│   │       ├── components/
│   │       ├── lib/
│   │       ├── share/
│   │       ├── vocab.txt
│   │       └── converters/
│   └── packs/
│       └── <pack_id>/...
│
├── user_data/
│   ├── audit/
│   ├── permissions/
│   │   ├── approvals/
│   │   ├── network/
│   │   ├── capabilities/
│   │   └── .secret_key
│   ├── secrets/
│   ├── packs/
│   ├── capabilities/
│   │   ├── handlers/
│   │   ├── trust/
│   │   └── requests/
│   ├── pip/
│   ├── pack_staging/
│   ├── pack_backups/
│   ├── shared/
│   │   └── flows/
│   │       └── modifiers/
│   ├── pending/
│   │   └── summary.json
│   ├── stores/
│   └── settings/
│       ├── shared_dict/
│       └── lib_execution_records.json
│
├── rumi_setup/
│   ├── core/
│   ├── cli/
│   ├── web/
│   ├── guide/
│   └── defaults/
│
├── lang/
│   ├── en.txt
│   └── ja.txt
│
├── tests/
│   ├── test_capability_installer.py
│   ├── test_capability_system.py
│   ├── test_ecosystem_phase1.py
│   ├── test_ecosystem_phase2.py
│   ├── test_ecosystem_phase3.py
│   ├── test_ecosystem_phase4.py
│   ├── test_ecosystem_phase5.py
│   ├── test_ecosystem_phase6.py
│   ├── test_egress_audit.py
│   ├── test_flow_resolution.py
│   ├── test_inbox_and_patches.py
│   ├── test_pip_installer.py
│   ├── test_secure_execution.py
│   └── test_shared_dict.py
│
└── docs/
    ├── architecture.md
    ├── pack-development.md
    ├── operations.md
    └── roadmap.md
</code></pre>

</details>

### 主要ディレクトリ

| ディレクトリ | 役割 |
|---|---|
| `core_runtime/` | カーネル — Flow 実行エンジン・セキュリティ・権限管理 |
| `core_runtime/shared_dict/` | 共有辞書システム（スナップショット・ジャーナル） |
| `core_runtime/core_pack/` | 公式 Capability 実装（Store, Secrets, Flow, Communication, Docker） |
| `backend_core/ecosystem/` | エコシステム基盤 — Pack/Component 読み込み・初期化 |
| `ecosystem/` | Pack 格納（外部供給物） |
| `user_data/` | 実行時永続データ（監査ログ・承認・Secrets・Store） |
| `rumi_setup/` | セットアップ支援（CLI / Web / ガイド） |
| `flows/` | 公式 Flow（起動・基盤） |
| `lang/` | 多言語メッセージ |
| `tests/` | テスト |
| `docs/` | ドキュメント |

### 主要ファイル

| ファイル | 役割 |
|---|---|
| `app.py` | OS エントリポイント |
| `bootstrap.py` | セットアップエントリポイント |
| `kernel.py` | Mixin 組み立て・ハンドラ登録 |
| `kernel_core.py` | Flow 実行エンジン本体 |
| `python_file_executor.py` | `python_file_call` 実行 |
| `secure_executor.py` | Docker 隔離実行 |
| `approval_manager.py` | Pack 承認管理 |
| `capability_proxy.py` | Capability Proxy サーバー（UDS） |
| `egress_proxy.py` | 外部通信プロキシ（UDS） |
| `flow_loader.py` | Flow YAML ローダー |
| `flow_modifier.py` | Flow modifier 適用 |
| `pack_importer.py` | Pack import（zip/folder → staging） |
| `pack_applier.py` | Pack apply（staging → ecosystem） |

## Workflow v4 graph compiler

The canonical frontend source for the control panel lives in `../tobkiri_launcher/frontend`.
`core_runtime/core_pack/core_control_panel/web` contains the built static artifact served by the kernel at `/panel/`.

Prompt behavior lives in `ecosystem/defaultspack/domain/prompt/` and `ecosystem/defaultspack/blocks/prompt/`. Tool behavior lives in `ecosystem/defaultspack/domain/tool/` and `ecosystem/defaultspack/blocks/tool/`. The old top-level `prompt/`, `tool/`, and `supporter/` import shims have been removed; new supporter-like behavior should be implemented as defaultspack functions, agents, prompts, memory, or extensions.

The retired `Flows.tsx` editor and legacy graph endpoints are not runtime
surfaces. Future graph-capable clients use the finite Workflow Pack
`graph.compile-preview` Contract operation. It converts a bounded
`rumi_graph` document into exact Workflow v4 `steps`, derives default editor
ports from the captured operation schema digests, and fails closed on missing
ports, incompatible edges, cycles, unreachable steps, or non-exact operation
identities. See [the graph compiler contract](docs/flow_graph_editor_todo.md)
for the simulation/runtime boundary and conformance fixtures.

## Basepack

`ecosystem/setup_pack/basepack/pack.json` を追加し、Rumi AI が graph-first のベース起動プロファイルとして `basepack` を選べるようにしました。現時点では既存の `defaultspack` を起動対象にする薄い bootstrap profile として扱い、巨大な複製 Pack を増やさず安全に導入しています。

---

## クイックスタート

### 必要条件

- Python 3.10+
- Docker（本番環境で必須）
- Git

### インストール

```bash
git clone https://github.com/harupipipipi/rumiai.git
cd rumiai/tobkiri_runtime
python bootstrap.py --cli init
```

### 起動

```bash
# 本番（Docker 必須）
python app.py

# 開発（Docker 不要）
python app.py --permissive
```

### Pack 承認

```bash
curl -X POST http://localhost:8765/api/packs/{pack_id}/approve \
  -H "Authorization: Bearer YOUR_TOKEN"
```

---

## ドキュメント

| ドキュメント | 内容 |
|---|---|
| [docs/architecture.md](docs/architecture.md) | 設計と仕組みの全体像 |
| [docs/pack-development.md](docs/pack-development.md) | Pack 開発ガイド |
| [docs/pack-development-guide.md](docs/pack-development-guide.md) | Pack 開発クイックスタート |
| [docs/operations.md](docs/operations.md) | 運用ガイド |
| [docs/roadmap.md](docs/roadmap.md) | ロードマップ |
| [docs/quality_pack/philosophy_memo.md](docs/quality_pack/philosophy_memo.md) | 開発判断に使う思想メモ |
| [docs/quality_pack/claude_desktop_quality_pack.md](docs/quality_pack/claude_desktop_quality_pack.md) | 品質保証・監査・回帰検証パック |

---

## ライセンス

MIT License
詳細はリポジトリルートの LICENSE を参照してください。
## defaultspack source of truth

The canonical defaultspack implementation in this repository is
`ecosystem/defaultspack/`. The older `ecosystem/defaults/` path and the separate
`harupipipipi/rumiai_defaults` repository are compatibility or snapshot sources.
New local-first runtime behavior should land in defaultspack, with legacy
aliases delegating back to it where needed.

The defaultspack runtime is designed to start without cloud API keys or external
network access. Its guaranteed default model is `stub/default`; cloud providers
are optional and must be selected/configured explicitly. Local file, terminal,
and git mutations are protected with local request guards, one-time signed
approval tokens, and redacted audit records.
