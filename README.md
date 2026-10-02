# Tobkiri

Tobkiri is a modular AI runtime and tooling workspace.

The project is being renamed from Rumi AI. The canonical Python package is
`tobkiri`; legacy internal paths, environment variables and installed
`tobkiri_runtime/rumi_ai` compatibility remain during the transition.

The runtime implementation lives under `tobkiri_runtime/`. The root `tobkiri/`
package delegates to that canonical package. The control panel frontend source
lives in `tobkiri_launcher/frontend`; the Launcher connects to its built artifact
at `/panel/`.

## Supported startup

Start **Tobkiri Launcher**, then activate a Defaults Profile and prepare the
PackVM through its setup screens. A fresh checkout is a native development
build, not a five-minute Python-only installation. Building the desktop shell,
preparing verified Defaults artifacts and provisioning a VM are separate steps.

- Already have an installer? Follow the [Launcher guide](./tobkiri_runtime/docs/tobkiri_launcher_start.md) for your platform
- Developing from source? Complete [Setup](#setup), then [Start](#start)
- Checking a running Host? Use the [health diagnostic](#backend-health-check)

`python -m tobkiri` deliberately fails closed without a Launcher-injected Pack
v4 activation snapshot. `python -m app` is an internal Host composition entrypoint,
not a standalone Defaults setup command. Neither command replaces the native
Launcher, verified Profile activation or PackVM provisioning. Opening `/panel/`
in a regular browser also does not establish native approval authority.

## Read This When...

| やりたいこと | まず読む場所 | 補足 |
|---|---|---|
| 目的別にドキュメントを辿りたい | [`tobkiri_runtime/docs/README.md`](./tobkiri_runtime/docs/README.md) | 「何をしたいか」から読む順番を案内します |
| 用語の意味を揃えたい | [`tobkiri_runtime/docs/terminology.md`](./tobkiri_runtime/docs/terminology.md) | `rule`, `skill`, `team workspace`, `subagent` 互換名の整理です |
| とにかく起動したい | [`README.md`](./README.md) の `Setup` と `Start` | Launcher の前提と起動手順を確認します |
| runtime / kernel の全体像を知りたい | [`tobkiri_runtime/README.md`](./tobkiri_runtime/README.md) | アーキテクチャと主要ディレクトリの説明があります |
| コードを読まずに仕組みを理解したい | [`tobkiri_runtime/docs/concepts/system-mechanism.md`](./tobkiri_runtime/docs/concepts/system-mechanism.md) | 起動・Flow・承認・Grant の流れを文章で追えます |
| まず動作確認したい（チュートリアル） | [`tobkiri_runtime/docs/tutorials/runtime-quickstart.md`](./tobkiri_runtime/docs/tutorials/runtime-quickstart.md) | Launcher 起動、Profile、PackVM、health を段階別に確認します |
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
- `tobkiri/`: repository entrypoint shim for the canonical Python package
- `tobkiri_runtime/rumi_ai/`: installed legacy Python compatibility package
- `pack-shell/`: desktop pack launcher
- `tobkiri_launcher/`: desktop shell and control panel frontend source
- `tobkiri_mobile/`: Flutter iOS/Android app for trusted-LAN defaultspack access
- `tobkiri_runtime/ecosystem/defaultspack/browser_extensions/`: browser companion assets bundled with defaultspack

## Setup

### Prerequisites for source development

- Git and Python 3.10+ with `venv` and `pip`
- Node.js **22.22.0 or newer** and npm, as required by the Launcher frontend
- Rust/Cargo and the **Cargo Tauri CLI 2.x** on `PATH`. The development hook
  invokes `cargo tauri` to build the Defaults Shell; installing only the npm
  Tauri CLI is insufficient
- `uv` 0.11.14, installed by the pinned Python development requirements below
- Platform-native [Tauri 2 build prerequisites](https://v2.tauri.app/start/prerequisites/):
  Xcode Command Line Tools on macOS; MSVC C++ Build Tools and WebView2 on Windows;
  WebKitGTK 4.1 and the listed development libraries on Linux
- At least 5 GiB of free disk space for the Launcher build preflight. Rust build
  artifacts, dependency caches and VM assets can require substantially more
- A clean, committed checkout whose generated source closure matches its
  manifest. Development preparation refuses to attest uncommitted source

If `cargo tauri --version` is unavailable, install the Cargo CLI as the repository's
installer workflow does:

```bash
cargo install tauri-cli --version "^2" --locked
cargo tauri --version
```

Flutter is needed only for `tobkiri_mobile` development.

### PackVM readiness

A working Launcher window or an activated Profile does not prove that a VM can
boot. Defaults Chat and Pack execution need verified VM assets, explicit
provisioning consent and a healthy attested PackVM on the actual device.
An ordinary developer build does not download or produce every platform's VM
assets. Use **Packs** → **PackVM lifecycle** and the
[Launcher guide](./tobkiri_runtime/docs/tobkiri_launcher_start.md#packvm-and-platform-readiness)
to inspect the reason when it reports **Not ready**. Do not replace the missing
VM with host execution or edit its attestation files.

### Clone and install

These steps currently target the Pack v4 development snapshot in
[PR #1496](https://github.com/harupipipipi/tobkiri/pull/1496), following
[PR #1322](https://github.com/harupipipipi/tobkiri/pull/1322). The repository's
default branch is not this snapshot. Fetch and select the PR head before
installing dependencies; do not mix these instructions with another revision.
The commands below create a new checkout and leave it at that exact fetched
commit. This is a development build, not a released installer.

Windows PowerShell:

```powershell
git clone https://github.com/harupipipipi/tobkiri.git
cd tobkiri
git fetch origin pull/1496/head
git switch --detach FETCH_HEAD

py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r tobkiri_runtime\requirements.txt
python -m pip install -r tobkiri_runtime\requirements-dev.txt
python -m pip install --no-deps -e .\tobkiri_runtime

cd tobkiri_launcher\frontend
npm ci
npm run tauri -- info
cd ..\..
```

If `py` is unavailable, use `python -m venv .venv`. If PowerShell blocks
activation, use `.\.venv\Scripts\python.exe` explicitly for the Python commands
and make `.venv\Scripts` available on `PATH` for the development tools; follow
your machine's execution-policy rules rather than changing them just to run this guide.

macOS / Linux:

```bash
git clone https://github.com/harupipipipi/tobkiri.git
cd tobkiri
git fetch origin pull/1496/head
git switch --detach FETCH_HEAD

python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r tobkiri_runtime/requirements.txt
python -m pip install -r tobkiri_runtime/requirements-dev.txt
python -m pip install --no-deps -e ./tobkiri_runtime

cd tobkiri_launcher/frontend
npm ci
npm run tauri -- info
cd ../..
```

## Start

First, from the repo root, check `git status --short` and resolve any tracked or
untracked source changes. If a build regenerates tracked frontend assets, review
and commit the intended output before retrying. Keep manifests and source from
the same revision; do not mark a modified checkout as clean.

macOS (explicit, ad-hoc **Tobkiri Launcher Developer** configuration):

```bash
source .venv/bin/activate
cd tobkiri_launcher/frontend
npm run tauri -- dev --config src-tauri/tauri.macos.dev.conf.json
```

Linux:

```bash
source .venv/bin/activate
cd tobkiri_launcher/frontend
npm run tauri -- dev
```

Windows PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
cd tobkiri_launcher\frontend
npm run tauri -- dev
```

Keep that terminal running for the development session. The Tauri hook builds
the panel and Defaults Shell, prepares the Defaults bundle, then starts the
native Launcher. The Launcher owns the Host bootstrap and panel connection;
do not start a second Host manually. `npm run dev` alone serves frontend code
and does not start the native Launcher or establish Authority.

In the Launcher:

1. Choose **Open Setup** if offered, review the Defaults Profile and confirm
   **Activate Defaults Profile** once. Wait for verification; use **Verify
   activation** if offered rather than submitting activation again
2. In **Packs**, inspect **PackVM lifecycle**. If a verified provisioning plan is
   offered, review its downloads, digests and storage requirements, then record
   explicit consent and provision it
3. Wait for **Healthy and attested**, then choose **Home** → **Launch Defaults
   Profile**. If setup or VM readiness fails, record the displayed reason

See the [Launcher start guide](./tobkiri_runtime/docs/tobkiri_launcher_start.md)
for installed apps, platform asset requirements and troubleshooting. A
successful source/import check, frontend build or installer workflow is not
proof that Defaults launched on your device.

## Common Tasks

### Just shortcuts

If you have `just` installed, common checks are available from the repo root:

```bash
just -l
just tooling-test
just integrity
```

### Backend health check

After the Launcher has started the Host, open a second terminal at the repo
root, activate the same `.venv`, and run:

```bash
python -m app --health
```

This probes `http://127.0.0.1:8765/health` without binding the port or starting a
Host. The port follows `RUMI_PORT`. `status: "down"` returns a nonzero exit code
when nothing is listening. Inspect `runtime_ready`, `needs_setup` and any
`runtime_error` in the response; a reachable Host does not prove Profile
activation, PackVM readiness or a usable Defaults window.

### Launcher development

For subsequent sessions with dependencies already installed, use the matching
platform command in [Start](#start). Source changes still require matching
artifacts and clean committed provenance. Keep the foreground development
process alive until you finish. A standalone Developer app must be prepared and
bundled through the proper native build workflow; copying a debug executable or
opening a leftover app is not equivalent to `tauri dev`.

The preflight error may still use the compatibility text `Rumi Viewer build
preflight failed: not enough free disk space.` Free disk space before retrying;
passing a smaller threshold does not create room for the build or a VM.

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

The legacy `python -m rumi_ai migrate-hmac` subcommand was retired with the
Pack v4 composition root. The internal `app` parser accepts `--headless` and
`--health`; `--headless` does not supply a Launcher activation snapshot or bypass
setup. Use Launcher-driven activation rather than manually rewriting signed
configuration files.

## Components

- `tobkiri`: canonical Python package and CLI; root source shim delegates to it
- `tobkiri_runtime/rumi_ai`: installed legacy module compatibility
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
Host is listening. Start Tobkiri Launcher using [Start](#start).
`status: "error"` can indicate an HTTP error, an invalid response or a reported
unhealthy state. Inspect the complete JSON, including `error`, `runtime_status`
and `runtime_error`. The probe port follows `RUMI_PORT`.
```bash
# Confirm whether a Host is listening
lsof -nP -iTCP:8765 -sTCP:LISTEN
```

#### 2. Port 8765 already in use

**Problem**: Launcher Host startup reports "Address already in use".

**Solution**: Identify the listener first. Stop only the matching old Tobkiri/Rumi
process gracefully; do not use a forced kill for routine port cleanup.
```bash
# Find process using port 8765
lsof -nP -iTCP:8765 -sTCP:LISTEN

# After confirming the PID belongs to the old runtime
kill -TERM <PID>
```

#### 3. Launcher shows 401 error

**Problem**: Opening the panel shows 401 Unauthorized.

**Solution**: First check that an old kernel has not claimed port 8765 and that the
Launcher bootstrap secret belongs to the same runtime. Do not set an arbitrary API
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

**Solution**: Ensure you are at the repo root and using the `.venv` created in [Setup](#setup).
For a diagnostic that should not start the runtime, try `python -m app --help`.
An import succeeding does not establish Launcher activation.
```bash
source .venv/bin/activate
python -m pip install --no-deps -e ./tobkiri_runtime
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
