# Tobkiri

Tobkiri is a modular AI runtime and tooling workspace. `master` is the public,
stable entry point; current development is on
[`soon`](https://github.com/harupipipipi/tobkiri/tree/soon).

> **Compatibility note**: the `rumi_ai_1_10/` directory and `python -m rumi_ai`
> entry point are retained on this branch for runtime compatibility. They are
> internal legacy identifiers, not the Tobkiri product name.

The canonical control panel frontend source lives in `rumi_viewer/frontend`; the
kernel serves its built artifact at `/panel/`.

## Read This When...

| やりたいこと | まず読む場所 | 補足 |
|---|---|---|
| 目的別にドキュメントを辿りたい | [`rumi_ai_1_10/docs/README.md`](./rumi_ai_1_10/docs/README.md) | 「何をしたいか」から読む順番を案内します |
| 用語の意味を揃えたい | [`rumi_ai_1_10/docs/terminology.md`](./rumi_ai_1_10/docs/terminology.md) | `rule`, `skill`, `team workspace`, `subagent` 互換名の整理です |
| とにかく起動したい | [`README.md`](./README.md) の `Start` | 最短の起動コマンドだけを載せています |
| runtime / kernel の全体像を知りたい | [`rumi_ai_1_10/README.md`](./rumi_ai_1_10/README.md) | アーキテクチャと主要ディレクトリの説明があります |
| コードを読まずに仕組みを理解したい | [`rumi_ai_1_10/docs/concepts/system-mechanism.md`](./rumi_ai_1_10/docs/concepts/system-mechanism.md) | 起動・Flow・承認・Grant の流れを文章で追えます |
| まず動作確認したい（チュートリアル） | [`rumi_ai_1_10/docs/tutorials/runtime-quickstart.md`](./rumi_ai_1_10/docs/tutorials/runtime-quickstart.md) | `--health` から `/panel/` まで最短手順です |
| `rumi_viewer` を起動したい / viewer の詰まり方を見たい | [`rumi_ai_1_10/docs/rumi_viewer_start.md`](./rumi_ai_1_10/docs/rumi_viewer_start.md) | 起動手順、`401`, 黒画面, `defaultspack` との関係をまとめています |
| viewer 側を直したい | [`rumi_viewer/src-tauri/src/config.rs`](./rumi_viewer/src-tauri/src/config.rs) と [`rumi_viewer/src-tauri/src/kernel_manager.rs`](./rumi_viewer/src-tauri/src/kernel_manager.rs) | viewer は Tauri shell、kernel 起動は Rust 側が担当です |
| pack / defaultspack を触りたい | [`rumi_ai_1_10/ecosystem/defaultspack/README.md`](./rumi_ai_1_10/ecosystem/defaultspack/README.md) | chat, ai_client, tool などの pack 側実装です |
| defaultspack の frontend 拡張方法を知りたい | [`rumi_ai_1_10/ecosystem/defaultspack/docs/frontend_extensions.md`](./rumi_ai_1_10/ecosystem/defaultspack/docs/frontend_extensions.md) | 右バー追加、設定追加、chat renderer 拡張、preview feed 追加の入り口です |
| API キーや secrets の扱いを知りたい | [`rumi_ai_1_10/docs/operations.md`](./rumi_ai_1_10/docs/operations.md) の Secrets 節 | `user_data/secrets/` と API 経路の説明があります |
| Pack の作り方を知りたい | [`rumi_ai_1_10/docs/pack-development.md`](./rumi_ai_1_10/docs/pack-development.md) | ecosystem.json, routes, permissions の作法をまとめています |
| 運用・監査の考え方を知りたい | [`rumi_ai_1_10/docs/quality_pack/philosophy_memo.md`](./rumi_ai_1_10/docs/quality_pack/philosophy_memo.md) | 継続開発と回帰確認の前提を整理しています |

## Repository Layout

- `rumi_ai_1_10/`: compatibility-named kernel/runtime/API/backend source tree
- `rumi_ai/`: compatibility Python entrypoint package
- `pack-shell/`: desktop pack launcher
- `rumi_viewer/`: desktop shell and control panel frontend source
- `rumi_ai_1_10/ecosystem/rumi_mobile/`: Flutter iOS/Android app for trusted-LAN defaultspack access
- `rumi_ai_1_10/ecosystem/defaultspack/browser_extensions/`: browser companion assets bundled with defaultspack

## Setup

### Prerequisites

- Python 3.10+
- Node.js 20.19.x または 22.12+（Node 22 推奨）
- npm
- uv (`rumi_viewer` を触る場合)
- Rust / Cargo (`rumi_viewer` を触る場合)
- Flutter SDK (`rumi_ai_1_10/ecosystem/rumi_mobile` を触る場合)

### Clone and install

```bash
git clone https://github.com/harupipipipi/tobkiri.git
cd tobkiri

python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r rumi_ai_1_10/requirements.txt
pip install -r rumi_ai_1_10/requirements-dev.txt
pip install -e ./rumi_ai_1_10

cd rumi_viewer/frontend
npm ci
cd ../..
```

## Start

```bash
source .venv/bin/activate
python -m rumi_ai --health
python -m rumi_ai
```

`--health` はシステムボリューム使用率も確認します。`disk` probe が `DEGRADED` / `DOWN` の場合は、コード不具合ではなく空き容量不足の可能性があります。

## Common Tasks

### Just shortcuts

If you have `just` installed, common checks are available from the repo root:

```bash
just -l
just tooling-test
just integrity
```

### Backend health check

```bash
python -m rumi_ai --health
```

### Runtime startup

```bash
python -m rumi_ai
```

### Viewer development

```bash
cd rumi_viewer/frontend
npm install
cd ..
cargo tauri dev
```

2 回目以降、`rumi_viewer/frontend/node_modules` が残っている場合は次だけで起動できます。

```bash
cd rumi_viewer
cargo tauri dev
```

開発用 viewer は repo 内の `rumi_ai_1_10/` を自動検出して kernel を起動します。
Viewer build は起動前に空き容量を確認します。`Rumi Viewer build preflight failed: not enough free disk space.` が出た場合はディスク容量を空けてから再実行してください。検証済みの環境で閾値だけを調整したい場合は `RUMI_VIEWER_MIN_FREE_MB=<MB>` を指定できます。
`Open Defaultspack` は開発起動では repo 同梱の `defaultspack` を優先して開きます。
起動時の詰まり方を含めたガイドは [`rumi_ai_1_10/docs/rumi_viewer_start.md`](./rumi_ai_1_10/docs/rumi_viewer_start.md) を参照してください。

## Development

```bash
source .venv/bin/activate
cd rumi_ai_1_10
python -m pytest tests/test_capability_trust_store.py
```

## Quality Pack

継続開発・監査・回帰確認の運用パックは以下を参照:

- `rumi_ai_1_10/docs/quality_pack/philosophy_memo.md`
- `rumi_ai_1_10/docs/quality_pack/claude_desktop_quality_pack.md`
- `rumi_ai_1_10/scripts/quality_pack/run_claude_quality_pack.sh`

## HMAC Migration

```bash
python -m rumi_ai migrate-hmac
```

## Components

- `rumi_ai`: compatibility CLI and module entrypoint
- `rumi_ai_1_10`: compatibility-named kernel, runtime, API, backend, and docs
- `pack-shell`: launches desktop packs and brokers token/bootstrap flow
- `rumi_viewer`: viewer-side application shell and canonical panel frontend source
- `rumi_ai_1_10/ecosystem/rumi_mobile`: mobile remote client for the bearer-auth Kernel Pack API
- `rumi_ai_1_10/ecosystem/defaultspack/browser_extensions/rumi_browser_companion`: unpacked Chromium extension for the defaultspack `browser_companion` tool

For architecture and runtime details, see [rumi_ai_1_10/README.md](./rumi_ai_1_10/README.md).

For Codex OSS-inspired coding-tool conventions, see [AGENTS.md](./AGENTS.md) and
[rumi_ai_1_10/docs/codex_oss_reference.md](./rumi_ai_1_10/docs/codex_oss_reference.md).
