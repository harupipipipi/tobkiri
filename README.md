# Tobkiri

Tobkiri is a modular AI runtime and tooling workspace.

The project is being renamed from Rumi AI. Existing package names, commands,
paths, environment variables, and application identifiers remain unchanged
during the compatibility transition.

The repository keeps the runtime implementation under `tobkiri_runtime/`, while `rumi_ai/` provides a version-stable Python entrypoint. The canonical control panel frontend source lives in `tobkiri_launcher/frontend`; the kernel serves its built artifact at `/panel/`.

## Quick Start (5 minutes)

Get Tobkiri running in 5 minutes:

```bash
# 1. Clone the repository
git clone https://github.com/harupipipipi/tobkiri.git
cd tobkiri

# 2. Set up Python environment
python3 -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
python -m pip install --upgrade pip

# 3. Install dependencies
pip install -r tobkiri_runtime/requirements.txt
pip install -r tobkiri_runtime/requirements-dev.txt
pip install -e ./tobkiri_runtime

# 4. Start the runtime (keeps running; blocks this terminal)
python -m app

# 5. In a second terminal, check the running Host's health
python -m app --health
```

`python -m app --health` probes `http://127.0.0.1:8765/health` on the already-running Host. It never binds the listen port itself, so it is safe to run while an instance is up, and it exits non-zero when no Host is listening.

After starting, open http://localhost:8765/panel/ in your browser to access the control panel.

> `python -m rumi_ai` is a compatibility shim, not a startup command. Outside the repo checkout it resolves to `tobkiri_runtime/rumi_ai`, which intentionally fails closed with `Tobkiri requires a Launcher-injected Pack v4 activation snapshot` — the Pack v4 Host can only be activated by the real composition root (`python -m app`) or by Tobkiri Launcher.

## Read This When...

| やりたいこと | まず読む場所 | 補足 |
|---|---|---|
| 目的別にドキュメントを辿りたい | [`tobkiri_runtime/docs/README.md`](./tobkiri_runtime/docs/README.md) | 「何をしたいか」から読む順番を案内します |
| 用語の意味を揃えたい | [`tobkiri_runtime/docs/terminology.md`](./tobkiri_runtime/docs/terminology.md) | `rule`, `skill`, `team workspace`, `subagent` 互換名の整理です |
| とにかく起動したい | [`README.md`](./README.md) の `Start` | 最短の起動コマンドだけを載せています |
| runtime / kernel の全体像を知りたい | [`tobkiri_runtime/README.md`](./tobkiri_runtime/README.md) | アーキテクチャと主要ディレクトリの説明があります |
| コードを読まずに仕組みを理解したい | [`tobkiri_runtime/docs/concepts/system-mechanism.md`](./tobkiri_runtime/docs/concepts/system-mechanism.md) | 起動・Flow・承認・Grant の流れを文章で追えます |
| まず動作確認したい（チュートリアル） | [`tobkiri_runtime/docs/tutorials/runtime-quickstart.md`](./tobkiri_runtime/docs/tutorials/runtime-quickstart.md) | `--health` から `/panel/` まで最短手順です |
| `tobkiri_launcher` を起動したい / viewer の詰まり方を見たい | [`tobkiri_runtime/docs/tobkiri_launcher_start.md`](./tobkiri_runtime/docs/tobkiri_launcher_start.md) | 起動手順、`401`, 黒画面, `defaultspack` との関係をまとめています |
| macOS版の配布方式と制約を知りたい | [`tobkiri_runtime/docs/macos-unsigned-distribution.md`](./tobkiri_runtime/docs/macos-unsigned-distribution.md) | unsigned/ad-hoc配布、Gatekeeper、quarantine、TCCの前提を説明します |
| viewer 側を直したい | [`tobkiri_launcher/src-tauri/src/config.rs`](./tobkiri_launcher/src-tauri/src/config.rs) と [`tobkiri_launcher/src-tauri/src/kernel_manager.rs`](./tobkiri_launcher/src-tauri/src/kernel_manager.rs) | viewer は Tauri shell、kernel 起動は Rust 側が担当です |
| pack / defaultspack を触りたい | [`tobkiri_runtime/ecosystem/defaultspack/README.md`](./tobkiri_runtime/ecosystem/defaultspack/README.md) | chat, ai_client, tool などの pack 側実装です |
| defaultspack の frontend 拡張方法を知りたい | [`tobkiri_runtime/ecosystem/defaultspack/docs/frontend_extensions.md`](./tobkiri_runtime/ecosystem/defaultspack/docs/frontend_extensions.md) | 右バー追加、設定追加、chat renderer 拡張、preview feed 追加の入り口です |
| API キーや secrets の扱いを知りたい | [`tobkiri_runtime/docs/operations.md`](./tobkiri_runtime/docs/operations.md) の Secrets 節 | `user_data/secrets/` と API 経路の説明があります |
| Pack の作り方を知りたい | [`tobkiri_runtime/docs/pack-development.md`](./tobkiri_runtime/docs/pack-development.md) | ecosystem.json, routes, permissions の作法をまとめています |
| 運用・監査の考え方を知りたい | [`tobkiri_runtime/docs/quality_pack/philosophy_memo.md`](./tobkiri_runtime/docs/quality_pack/philosophy_memo.md) | 継続開発と回帰確認の前提を整理しています |

## Repository Layout

- `tobkiri_runtime/`: kernel/runtime/API/backend source tree
- `rumi_ai/`: compatibility Python entrypoint package
- `pack-shell/`: desktop pack launcher
- `tobkiri_launcher/`: desktop shell and control panel frontend source
- `tobkiri_mobile/`: Flutter iOS/Android app for trusted-LAN defaultspack access
- `tobkiri_runtime/ecosystem/defaultspack/browser_extensions/`: browser companion assets bundled with defaultspack

## Setup

### Prerequisites

- Python 3.10+
- Node.js 22.22+（Node 22 LTS 推奨）
- npm
- uv (`tobkiri_launcher` を触る場合)
- Rust / Cargo (`tobkiri_launcher` を触る場合)
- Xcode command line tools / Swift（Apple Silicon macOS のデスクトップ起動）
- MSVC Build Tools (`tobkiri_launcher` を Windows で触る場合)
- Flutter SDK (`tobkiri_mobile` を触る場合)

### Dockerless sandbox on macOS

Apple Silicon macOS uses the native Virtualization.framework PackVM. Docker
and Lima are not required. The desktop command below builds Tobkiri Launcher,
its Defaults Shell, and the PackVM helper from the checkout. It signs the local
application and helper ad hoc, verifies their manifests, and then starts the
application. No signing certificate or cloud API key is required.

Allow at least 17 GiB of free space before the first build and PackVM setup.
This includes build space, the pinned 3 GiB Debian image, and separate isolated
domains for the application UI and the first saved conversation. The build
and provisioning flows check available space before proceeding. PackVM setup
downloads the image after you approve the displayed plan in the Launcher.
Isolated Pack operations run inside the VM and its per-operation sandbox.
Approved, shipped Host extensions provide approval, durable storage, model
connections, and OS services in the host process; they are trusted host code,
not VM-isolated code. External Packs cannot declare themselves Host extensions.
An unavailable PackVM does not fall back to host execution. Local ad-hoc
signatures identify this development build, not a verified release.

### Clone and install

Windows PowerShell:

```powershell
git clone https://github.com/harupipipipi/tobkiri.git
cd tobkiri

py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r tobkiri_runtime\requirements.txt
python -m pip install -r tobkiri_runtime\requirements-dev.txt
python -m pip install -e .\tobkiri_runtime

cd tobkiri_launcher\frontend
npm ci
npm run tauri -- info
cd ..\..
```

If `py` is not available, use `python -m venv .venv` instead. If PowerShell blocks `Activate.ps1`, run `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass` in the same terminal, then activate the venv again.

macOS / Linux:

```bash
git clone https://github.com/harupipipipi/tobkiri.git
cd tobkiri

python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r tobkiri_runtime/requirements.txt
python -m pip install -r tobkiri_runtime/requirements-dev.txt
python -m pip install -e ./tobkiri_runtime

cd tobkiri_launcher/frontend
npm ci
npm run tauri -- info
cd ../..
```

## Start

Windows PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
python -m app --health
cd tobkiri_launcher\frontend
npm run tauri -- dev
```

Apple Silicon macOS (complete desktop, without Docker):

```bash
source .venv/bin/activate
cd tobkiri_launcher/frontend
npm run desktop
```

After the Launcher is ready, check it from a second terminal in the repo root:

```bash
source .venv/bin/activate
python -m app --health
```

`npm run desktop` builds and starts **Tobkiri Launcher Developer.app** with its
native PackVM helper. Keep the terminal open while using the app. For later
starts without source changes, open the built app from the repo root:

```bash
open "tobkiri_launcher/src-tauri/target/aarch64-apple-darwin/debug/bundle/macos/Tobkiri Launcher Developer.app"
```

Keep the checkout and its `.venv` in place; this development app uses them.
After source changes, commit them and run `npm run desktop` again.
The raw `npm run tauri -- dev` command is for Launcher
UI development; it does not bundle the PackVM helper needed to run Defaults.

When the Launcher window opens, complete setup if prompted. On Apple Silicon macOS, open **Packs** → **Tobkiri Host Pack Control** → **PackVM lifecycle**, prepare and approve the plan, and provision PackVM. Once it reports **Healthy and attested**, use **Home** → **Launch Tobkiri Defaults** to open Defaultspack. The current Windows build can activate the Profile but cannot launch Defaultspack Chat or Pack functions because its PackVM backend is unfinished; see the [Launcher start guide](./tobkiri_runtime/docs/tobkiri_launcher_start.md) and [Windows support issue #1494](https://github.com/harupipipipi/tobkiri/issues/1494). `python -m app` is useful for starting or checking the kernel, but it does not replace Launcher and PackVM setup.

To open `http://127.0.0.1:8766/p/defaults/chat` in a browser, select **アクセスをリクエスト** and approve the separate Tobkiri Launcher window. The window shows the exact local origin, Profile and route. Approval expires after two minutes and applies only to the browser that requested access. Closing the window or selecting **拒否** denies the request. The Host owns the pending request and login session; the Launcher owns the local approval window; Defaults presents the chat after login. This login does not approve tool execution. Existing tool approval, capability and workspace policies still apply. Launcher must be running with an active Defaults Profile; unavailable or stale runtimes cannot issue a session.

`--health` は起動中の Host の `/health` endpoint を probe します。Host が未起動の場合は `status: "down"` と非ゼロの exit code を返すので、先に `python -m app` または Launcher で kernel を起動してください。

### Use a local model without Docker or an API key

For example, run a 1B model with llama.cpp on Apple Silicon macOS:

```bash
brew install llama.cpp
llama-server -hf ggml-org/gemma-3-1b-it-GGUF:Q4_K_M \
  --host 127.0.0.1 --port 1234 --alias gemma-3-1b-it \
  --ctx-size 16384 --parallel 1 --jinja --temp 0 --seed 42
```

The first run downloads the model; later runs use the downloaded file.
This example fixes sampling for repeatable local smoke tests.
In Tobkiri, open **Settings** → **Models**, select **Custom**, then choose
**Local OpenAI-compatible**. Enter a connection name and
`http://127.0.0.1:1234/v1`, and approve the connection when prompted.
No API key is required. Create a model route using that registered connection
and model ID `gemma-3-1b-it`, then select the saved model in the chat composer.

Local connections accept only numeric loopback addresses (`127.0.0.1` or
`[::1]`), an explicit port from 1024 to 65535, and the `/v1` base path.
Keep the model server running while chatting. Hosted providers continue to
require HTTPS and an API key.

Submitted messages appear in the conversation immediately with a small clock
beside the text. The clock changes to a single check mark when the saved
message is confirmed. The check mark indicates saved delivery, not a read
receipt. A status check that is still pending does not mean the message failed
to send.
The input remains visible as soon as you submit a new conversation. It briefly
shows `会話を準備しています。` with its controls disabled while the conversation
and saved turn are registered. Stop and additional instructions become
available only after the saved turn is readable from its owner; assigning a
local request ID alone does not enable them.
The new-chat welcome screen and input also remain visible after a confirmed
stop, even when the native window pauses its animations.
A Stop acknowledgement remains pending until the execution result is confirmed.
If the reply was already saved, its verified result completes the turn instead
of remaining stuck in reconciliation. Switching chats does not carry another
chat's generating state into the selected chat's input.

If a saved turn fails before either message is persisted, the error stays
visible. If you have not changed the composer since sending and it is still
empty, the submitted text is restored. It is never sent again automatically.
`PACKVM_HOST_CAPACITY_INSUFFICIENT`
means the host needs more free disk space before a conversation VM can start;
free space, then retry the restored message.

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
python -m app --health
```

### Runtime startup

```bash
python -m app
```

### Viewer development

```bash
cd tobkiri_launcher/frontend
npm install
npm run tauri -- info
npm run tauri -- dev
```

2 回目以降、`tobkiri_launcher/frontend/node_modules` が残っている場合は次だけで起動できます。

```bash
cd tobkiri_launcher/frontend
npm run tauri -- dev
```

開発用 Tobkiri Launcher は repo 内の `tobkiri_runtime/` を自動検出して kernel を起動します。
開発用 Defaults バンドルを準備する前に、ソース変更をコミットして作業ツリーをクリーンにしてください。ビルド元のコミットと実際のソースが一致しない場合、準備処理は停止します。
Tauri の起動前処理は Python 3 を使います。macOS/Linux では `python3` を優先し、Windows では Python Launcher (`py -3`) を優先するため、`python` という別名を作る必要はありません。
Viewer build は起動前に空き容量を確認します。`Rumi Viewer build preflight failed: not enough free disk space.` が出た場合はディスク容量を空けてから再実行してください。検証済みの環境で閾値だけを調整したい場合は `RUMI_VIEWER_MIN_FREE_MB=<MB>` を指定できます。
Apple Silicon macOS で **Launch Defaults Profile** を使うと、開発起動では repo 同梱の `defaultspack` を優先して開きます。Windows の現行ビルドは PackVM を準備できないため、Chat や Pack 実行は起動できません。
起動時の詰まり方を含めたガイドは [`tobkiri_runtime/docs/tobkiri_launcher_start.md`](./tobkiri_runtime/docs/tobkiri_launcher_start.md) を参照してください。

## Development

```bash
source .venv/bin/activate
cd tobkiri_runtime
python -m pytest tests/test_capability_trust_store.py
```

## Quality Pack

継続開発・監査・回帰確認の運用パックは以下を参照:

- `tobkiri_runtime/docs/quality_pack/philosophy_memo.md`
- `tobkiri_runtime/docs/quality_pack/claude_desktop_quality_pack.md`
- `tobkiri_runtime/scripts/quality_pack/run_claude_quality_pack.sh`

## HMAC Migration

The legacy `python -m rumi_ai migrate-hmac` subcommand was retired with the Pack v4 composition root. `python -m app` accepts only `--headless` and `--health`; unsigned configuration files are re-signed during Launcher-driven activation.

## Components

- `rumi_ai`: compatibility CLI and module entrypoint
- `tobkiri_runtime`: kernel, runtime, API, backend, and docs
- `pack-shell`: launches desktop packs and brokers token/bootstrap flow
- `tobkiri_launcher`: viewer-side application shell and canonical panel frontend source
- `tobkiri_mobile`: mobile remote client for the bearer-auth Kernel Pack API
- `tobkiri_runtime/ecosystem/defaultspack/browser_extensions/rumi_browser_companion`: unpacked Chromium extension for the defaultspack `browser_companion` tool

## Troubleshooting

### Common Issues

#### 1. Health check reports `status: "down"` or `status: "error"`

**Problem**: `python -m app --health` exits non-zero.

**Solution**: `--health` only probes an already-running Host at
`http://127.0.0.1:8765/health`; it never starts one. `status: "down"` means no
Host is listening — start it with `python -m app` or Tobkiri Launcher.
`status: "error"` means a Host answered but reports an unhealthy runtime; check
the `runtime_status` / `runtime_error` fields in the JSON output. The probe port
follows `RUMI_PORT`.
```bash
# Confirm whether a Host is listening
lsof -nP -iTCP:8765 -sTCP:LISTEN
```

#### 2. Port 8765 already in use

**Problem**: `python -m app` fails with "Address already in use".

**Solution**: Identify the listener first. Stop only the matching old Tobkiri/Rumi
process gracefully; do not use a forced kill for routine port cleanup.
```bash
# Find process using port 8765
lsof -nP -iTCP:8765 -sTCP:LISTEN

# After confirming the PID belongs to the old runtime
kill -TERM <PID>
```

#### 3. Viewer shows 401 error

**Problem**: Opening the panel shows 401 Unauthorized.

**Solution**: First check that an old kernel has not claimed port 8765 and that the
viewer bootstrap secret belongs to the same runtime. Do not set an arbitrary API
token to work around a bootstrap failure.
```bash
lsof -nP -iTCP:8765 -sTCP:LISTEN
pgrep -fl 'tobkiri|rumi_ai|python.*-m app'
```

#### 4. Frontend build fails

**Problem**: `npm run build` fails in tobkiri_launcher/frontend.

**Solution**: Keep `package-lock.json` and install its pinned dependency graph.
```bash
cd tobkiri_launcher/frontend
npm ci
npm run build
```

#### 5. Python import errors

**Problem**: `ModuleNotFoundError` when running tests.

**Solution**: Ensure you're in the virtual environment and package is installed.
```bash
source .venv/bin/activate
pip install -e ./tobkiri_runtime
```

### Getting Help

If you encounter issues not covered here:

1. Check the [documentation](./tobkiri_runtime/docs/README.md)
2. Search existing [GitHub Issues](https://github.com/harupipipipi/tobkiri/issues)
3. Create a new issue with:
   - Steps to reproduce
   - Expected behavior
   - Actual behavior
   - Error messages/logs

## Contributing

We welcome contributions! Please follow these guidelines:

### Development Workflow

1. Fork the repository
2. Create a feature branch: `git checkout -b feature/your-feature`
3. Make your changes
4. Run tests: `just tooling-test`
5. Run linting: `just lint`
6. Commit your changes: `git commit -m 'Add your feature'`
7. Push to the branch: `git push origin feature/your-feature`
8. Create a Pull Request

### Code Style

- Python: Follow PEP 8, use type hints
- JavaScript/TypeScript: Run the repository's `npm run lint` checks
- Rust: Follow rustfmt defaults

### Testing

- Add tests for new features
- Ensure existing tests pass
- Run focused tests: `python -m pytest tests/test_specific.py -q`

### Pull Request Guidelines

- Use the PR template provided
- Include a clear description
- Reference related issues
- Ensure CI passes

### Security

- Never commit API keys or secrets
- Follow security guidelines in [AGENTS.md](./AGENTS.md)
- Report security issues privately

## License

This project is licensed under the terms specified in [LICENSE](./LICENSE).

For architecture and runtime details, see [tobkiri_runtime/README.md](./tobkiri_runtime/README.md).

For Codex OSS-inspired coding-tool conventions, see [AGENTS.md](./AGENTS.md) and
[tobkiri_runtime/docs/codex_oss_reference.md](./tobkiri_runtime/docs/codex_oss_reference.md).
