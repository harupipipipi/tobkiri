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

# 4. Run health check
python -m rumi_ai --health

# 5. Start the runtime
python -m rumi_ai
```

After starting, open http://localhost:8765/panel/ in your browser to access the control panel.

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
- Node.js 20.19.x または 22.12+（Node 22 推奨）
- npm
- uv (`tobkiri_launcher` を触る場合)
- Rust / Cargo (`tobkiri_launcher` を触る場合)
- MSVC Build Tools (`tobkiri_launcher` を Windows で触る場合)
- Flutter SDK (`tobkiri_mobile` を触る場合)

### Dockerless sandbox on macOS

Docker is optional on macOS. Tobkiri uses a managed Lima VM for untrusted
function, Pack process, and coding-terminal execution:

```bash
brew install lima
```

Open the runtime setup flow once after installation. Tobkiri creates an Ubuntu
VM with no host mounts, SSH agent forwarding, proxy propagation, containerd, or
guest port forwarding. Each untrusted operation then runs inside an additional
Bubblewrap user/PID/filesystem/network namespace in that VM. Network is denied
unless the calling tool explicitly receives an approved network policy.

The first setup downloads an Ubuntu image and installs the guest packages.
Desktop GUI applications share the managed guest identity, so the GUI desktop
is not itself an untrusted application boundary; Pack and terminal commands use
the per-operation boundary described above.

### Clone and install

Windows PowerShell:

```powershell
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
python -m rumi_ai --health
cd tobkiri_launcher\frontend
npm run tauri -- dev
```

When the viewer window opens, complete setup if prompted, then use Home -> `Open Defaultspack` to launch the defaultspack UI. `python -m rumi_ai` is useful for starting or checking the kernel, but the fresh-user desktop path for defaultspack is through the viewer button, not a manual port-8766 launch.

macOS / Linux:

```bash
source .venv/bin/activate
python -m rumi_ai --health
cd tobkiri_launcher/frontend
npm run tauri -- dev
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
`Open Defaultspack` は開発起動では repo 同梱の `defaultspack` を優先して開きます。
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

```bash
python -m rumi_ai migrate-hmac
```

## Components

- `rumi_ai`: compatibility CLI and module entrypoint
- `tobkiri_runtime`: kernel, runtime, API, backend, and docs
- `pack-shell`: launches desktop packs and brokers token/bootstrap flow
- `tobkiri_launcher`: viewer-side application shell and canonical panel frontend source
- `tobkiri_mobile`: mobile remote client for the bearer-auth Kernel Pack API
- `tobkiri_runtime/ecosystem/defaultspack/browser_extensions/rumi_browser_companion`: unpacked Chromium extension for the defaultspack `browser_companion` tool

## Troubleshooting

### Common Issues

#### 1. Health check fails with "disk probe DEGRADED/DOWN"

**Problem**: `python -m rumi_ai --health` shows disk probe as DEGRADED or DOWN.

**Solution**: This is usually a disk space issue, not a code problem. Identify the
largest workspace or build artifacts first; recreating a virtual environment can
consume additional disk space.
```bash
# Check disk space
df -h

# Inspect large local artifacts before removing anything
du -sh .venv node_modules tobkiri_launcher/frontend/node_modules 2>/dev/null
```

#### 2. Port 8765 already in use

**Problem**: `python -m rumi_ai` fails with "Address already in use".

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
