# Draft checkpoint staging audit, 2026-10-15

This is a source hygiene and staging-scope audit. The migration goal remains
active and incomplete. This report does not establish product acceptance, a
current passing workspace/CI run, final native behavior or migration completion.
No Git/index changes, Cargo commands, builds, tests, installations or downloads
were performed by this audit. Only this report was written.

## Exact audited scope

The snapshot contains **869 source/document candidates**, comprising
14 tracked changes and
855 untracked files, with
**29,027,630 bytes**. These figures exclude this newly written report.
The proposed staging set below adds this report for **870 paths**.
There were 0 staged paths when checked.

Concurrent source updates were observed during this audit. The candidate path
set stayed stable, but the content fingerprint is a measured snapshot, not an
established content freeze. Root must review later byte changes before staging.

Every audited candidate is a regular UTF-8 text file. Full-file inspection found
zero NUL-containing files, binary/non-UTF-8 files, source symlinks or compiled
runtime/build/model/database/archive candidates. The largest file is the
intentional SQLite amalgamation `tools/wasm-sqlite/vendor/sqlite3.c`,
9,507,037 bytes. The two vendor `Cargo.toml.orig` files are upstream package
source metadata. Pi JavaScript bundles/maps are intentional source/build inputs
bound by the Pack manifests, rather than ignored Wasm runtime outputs.

| Top-level scope | Candidate files |
| --- | ---: |
| `.github` | 1 |
| `.gitignore` | 1 |
| `Cargo.lock` | 1 |
| `Cargo.toml` | 1 |
| `crates` | 188 |
| `docs` | 10 |
| `migration` | 6 |
| `packs` | 209 |
| `rust-toolchain.toml` | 1 |
| `sdk` | 64 |
| `tobkiri_launcher` | 36 |
| `tobkiri_runtime` | 4 |
| `tools` | 47 |
| `vectors` | 25 |
| `vendor` | 275 |

Source candidate-list SHA-256, sorted UTF-8 paths each followed by NUL:
`664c4ca433d0e0b457e415d734d5e37f09133e52c85c5ea3ba8218ce4decc2e7`.

Content-tree SHA-256, excluding this report:
`9e7b902f57b26971196947af741025c55449514380d6797e83ffa2b7621590ec`. The input is the concatenation, in sorted path order, of
`path + NUL + decimal_byte_length + NUL + lowercase_file_sha256 + LF`.
Later source changes need their own delta review; these fingerprints describe
this snapshot.

## Credentials and retained machine paths

No PEM private-key marker, production credential token or literal secret
assignment was found. The sole token-pattern hit,
`vectors/seals/approval-token.jsonl:29`, is a substring inside its deterministic
43-character HMAC fixture signature. All 1,802 exposed test key/seed/secret
fields equal the explicit public fixture sequence. The generators label these
as test-only data. `crates/tobkiri-store/tests/authority.rs:696` contains a
credential-bearing example URL in a negative authorization test, rather than a
saved credential. No private `.work` data or secret files were read.

The 14 committed example/published Profile JSONs now contain 85 relative local
`file:` locators and zero machine-absolute or URL source locators. The CLI
normalizer resolves these from the input document's directory and freezes an
absolute UTF-8 locator before Core approval/storage. This audit does not replace
Root's actual test receipt for the latest CLI source.

Remaining machine-path occurrences are classified as follows:

- `packs/local.pi-example-hello/wrapper.js.map:4` and
  `packs/local.pi-example-protected-paths/wrapper.js.map:4` each embed two build
  checkout paths in `sourcesContent` for generated `wrapper-entry.mjs` imports.
  Their `sources` arrays are relative; these embedded paths are debugging
  provenance, not runtime Pack source locators. Sanitizing their admitted bytes
  would require the normal Pack rebuild/hash update.
- `tools/legacy-vectors/freeze_reference.py:29`, `generate.py:27` and
  `generate_extra.py:29` retain a machine-specific frozen-legacy checkout
  default. These three development oracle defaults are **nonportable**;
  `COVERAGE.md:36–37` records matching local reproduction commands. They are not
  product runtime configuration. This source checkpoint retains them explicitly.
- `vectors/legacy-vectors.json:1763` records the historical oracle source tree.
  It is evidence provenance rather than an execution locator.
- The two shell-policy `inspection.json` files contain public classifier inputs
  such as Windows user paths; they are expected test strings. SQLite's vendored
  source/header contain upstream filesystem/URI examples. The `/private/var`
  mention in Launcher `build.rs:6853` explains a macOS temporary-directory alias;
  the SDK `/private/file` input is a rejection test. These are not saved user
  paths or private data.

No remaining versioned product/runtime machine-path blocker was found in this
scope. The nonportable development defaults above remain an explicit limitation.

## Staging plan and exclusions

Use only the literal paths below, including their current reviewed bytes.
Do not use force-add or a repository-wide add. Git ignore checks confirmed the
exclusions for root work/target/virtualenv paths, `.env`, Launcher
`core-host-ui/`, Pack `component.wasm`, `*.build.json`, generated WIT/guest WIT,
SDK `node_modules`/Python caches, runtime user data, bundled runtimes and Python
runtime outputs. The parent workspace's private `.work` receipts/models/tools
are outside this proposed set. Source fixtures under
`sdk/python/tests/fixtures/build/` are intentionally included.

The proposed **870-path** staging list SHA-256 is
`620cd6a4a537a87d6227a6bf9a36f4fa3e14982eb650b948a544c3b5c0e0c84a` using sorted UTF-8 paths followed by NUL.
After staging, compare the staged path set with this literal manifest. Do not
silently include later-created files or substitute ignored runtime artifacts.
Root owns Git operations and remaining validation/receipt updates. A clean
hygiene audit permits review of this draft source checkpoint; it does not close
its unfinished Core/provider/native/proc or broader migration acceptance work.

<!-- staging-paths:start -->
```text
.github/workflows/issue-1520-rust.yml
.gitignore
Cargo.lock
Cargo.toml
crates/test-guests/Cargo.lock
crates/test-guests/Cargo.toml
crates/test-guests/README.md
crates/test-guests/consumer/Cargo.toml
crates/test-guests/consumer/src/lib.rs
crates/test-guests/provider/Cargo.toml
crates/test-guests/provider/src/lib.rs
crates/test-guests/wit/deps/tobkiri-core/core.wit
crates/test-guests/wit/fixtures.wit
crates/tobkiri-canon/Cargo.toml
crates/tobkiri-canon/evidence/global-contracts-canonical-20261015.json
crates/tobkiri-canon/examples/freeze_launcher_seals.rs
crates/tobkiri-canon/src/codec.rs
crates/tobkiri-canon/src/files.rs
crates/tobkiri-canon/src/ids.rs
crates/tobkiri-canon/src/legacy.rs
crates/tobkiri-canon/src/lib.rs
crates/tobkiri-canon/src/ownership.rs
crates/tobkiri-canon/src/serde_value.rs
crates/tobkiri-canon/tests/codecs.rs
crates/tobkiri-canon/tests/extra_seals.rs
crates/tobkiri-canon/tests/ownership.rs
crates/tobkiri-canon/tests/vectors.rs
crates/tobkiri-cli/Cargo.toml
crates/tobkiri-cli/src/args.rs
crates/tobkiri-cli/src/commands.rs
crates/tobkiri-cli/src/lib.rs
crates/tobkiri-cli/src/main.rs
crates/tobkiri-cli/src/profile_input.rs
crates/tobkiri-cli/src/profile_input_tests.rs
crates/tobkiri-cli/src/terminal.rs
crates/tobkiri-core/Cargo.toml
crates/tobkiri-core/README.md
crates/tobkiri-core/src/admission.rs
crates/tobkiri-core/src/admission_tests.rs
crates/tobkiri-core/src/capacity.rs
crates/tobkiri-core/src/completion.rs
crates/tobkiri-core/src/core.rs
crates/tobkiri-core/src/core_tests.rs
crates/tobkiri-core/src/error.rs
crates/tobkiri-core/src/execution_frames.rs
crates/tobkiri-core/src/housekeeping.rs
crates/tobkiri-core/src/input_tests.rs
crates/tobkiri-core/src/inputs.rs
crates/tobkiri-core/src/journal.rs
crates/tobkiri-core/src/lib.rs
crates/tobkiri-core/src/outcomes.rs
crates/tobkiri-core/src/policy_tests.rs
crates/tobkiri-core/src/profiles.rs
crates/tobkiri-core/src/session.rs
crates/tobkiri-core/src/session/provider_entry.rs
crates/tobkiri-core/src/session/ui.rs
crates/tobkiri-core/src/session/ui_source.rs
crates/tobkiri-core/src/session_tests.rs
crates/tobkiri-core/src/test_support.rs
crates/tobkiri-core/src/tracked_stream.rs
crates/tobkiri-core/src/tracked_stream_tests.rs
crates/tobkiri-core/tests/journal.rs
crates/tobkiri-core/tests/provider_proxy.rs
crates/tobkiri-e2e/Cargo.toml
crates/tobkiri-e2e/README.md
crates/tobkiri-e2e/evidence/m2-final.json
crates/tobkiri-e2e/evidence/m4-pending.json
crates/tobkiri-e2e/evidence/native-input-entry-source-check-001.json
crates/tobkiri-e2e/evidence/native-input-entry-source-check-002.json
crates/tobkiri-e2e/evidence/native-input-entry-source-migration-001.json
crates/tobkiri-e2e/evidence/pi-producer-runtime-002.json
crates/tobkiri-e2e/evidence/python-current-parity-20261015.json
crates/tobkiri-e2e/evidence/python-outer-producer-pending-002.json
crates/tobkiri-e2e/evidence/python-source-preparation-001.json
crates/tobkiri-e2e/evidence/python-source-preparation-002.json
crates/tobkiri-e2e/evidence/sdk-dbapi-routing-003.json
crates/tobkiri-e2e/src/lib.rs
crates/tobkiri-e2e/tests/chat.rs
crates/tobkiri-e2e/tests/llama.rs
crates/tobkiri-e2e/tests/native_acceptance.rs
crates/tobkiri-e2e/tests/native_input.rs
crates/tobkiri-e2e/tests/network.rs
crates/tobkiri-e2e/tests/parity.rs
crates/tobkiri-e2e/tests/pi.rs
crates/tobkiri-e2e/tests/support/mod.rs
crates/tobkiri-e2e/tests/ui.rs
crates/tobkiri-e2e/tools/capture_m2.py
crates/tobkiri-flow/Cargo.toml
crates/tobkiri-flow/README.md
crates/tobkiri-flow/src/engine.rs
crates/tobkiri-flow/src/engine/execution.rs
crates/tobkiri-flow/src/execution_frame.rs
crates/tobkiri-flow/src/expression.rs
crates/tobkiri-flow/src/journal.rs
crates/tobkiri-flow/src/lib.rs
crates/tobkiri-flow/src/owned_expression.rs
crates/tobkiri-flow/src/run_stream.rs
crates/tobkiri-flow/src/stream.rs
crates/tobkiri-flow/tests/engine.rs
crates/tobkiri-flow/tests/support.rs
crates/tobkiri-gate/Cargo.toml
crates/tobkiri-gate/src/admission.rs
crates/tobkiri-gate/src/budget.rs
crates/tobkiri-gate/src/cancellation.rs
crates/tobkiri-gate/src/capacity.rs
crates/tobkiri-gate/src/context.rs
crates/tobkiri-gate/src/control.rs
crates/tobkiri-gate/src/engine.rs
crates/tobkiri-gate/src/frame.rs
crates/tobkiri-gate/src/ingress.rs
crates/tobkiri-gate/src/lib.rs
crates/tobkiri-gate/src/permissions.rs
crates/tobkiri-gate/src/protocol.rs
crates/tobkiri-manifest/Cargo.toml
crates/tobkiri-manifest/src/lib.rs
crates/tobkiri-manifest/src/package.rs
crates/tobkiri-manifest/src/shapes.rs
crates/tobkiri-manifest/src/wit_preflight.rs
crates/tobkiri-manifest/tests/contracts.rs
crates/tobkiri-manifest/tests/core_error_provenance.rs
crates/tobkiri-manifest/tests/directory_bounds.rs
crates/tobkiri-manifest/tests/shape_expansion.rs
crates/tobkiri-profile/Cargo.toml
crates/tobkiri-profile/src/compile.rs
crates/tobkiri-profile/src/lib.rs
crates/tobkiri-profile/src/wire.rs
crates/tobkiri-profile/tests/compiler.rs
crates/tobkiri-profile/tests/input_entry_compile.rs
crates/tobkiri-profile/tests/input_entry_wire.rs
crates/tobkiri-store/Cargo.toml
crates/tobkiri-store/src/approvals.rs
crates/tobkiri-store/src/audit.rs
crates/tobkiri-store/src/data.rs
crates/tobkiri-store/src/data_custody.rs
crates/tobkiri-store/src/data_move_tests.rs
crates/tobkiri-store/src/data_moves.rs
crates/tobkiri-store/src/disk_policy.rs
crates/tobkiri-store/src/lib.rs
crates/tobkiri-store/src/owner.rs
crates/tobkiri-store/src/quota_vfs.rs
crates/tobkiri-store/src/secure.rs
crates/tobkiri-store/src/sql.rs
crates/tobkiri-store/src/tiered.rs
crates/tobkiri-store/src/vault.rs
crates/tobkiri-store/src/vfs.rs
crates/tobkiri-store/src/vfs_unavailable.rs
crates/tobkiri-store/tests/admission.rs
crates/tobkiri-store/tests/authority.rs
crates/tobkiri-store/tests/native_quota.rs
crates/tobkiri-store/tests/owner.rs
crates/tobkiri-store/tests/owner_unavailable.rs
crates/tobkiri-store/tests/security.rs
crates/tobkiri-store/tests/terminal.rs
crates/tobkiri-wasm/CAPACITY.md
crates/tobkiri-wasm/Cargo.toml
crates/tobkiri-wasm/README.md
crates/tobkiri-wasm/build.rs
crates/tobkiri-wasm/src/abi.rs
crates/tobkiri-wasm/src/allocation.rs
crates/tobkiri-wasm/src/core_imports.rs
crates/tobkiri-wasm/src/dispatch.rs
crates/tobkiri-wasm/src/effect_values.rs
crates/tobkiri-wasm/src/effects.rs
crates/tobkiri-wasm/src/engine.rs
crates/tobkiri-wasm/src/entry.rs
crates/tobkiri-wasm/src/filesystem.rs
crates/tobkiri-wasm/src/filesystem_tests.rs
crates/tobkiri-wasm/src/handle_transfer.rs
crates/tobkiri-wasm/src/handles.rs
crates/tobkiri-wasm/src/host.rs
crates/tobkiri-wasm/src/imports.rs
crates/tobkiri-wasm/src/instance_capacity.rs
crates/tobkiri-wasm/src/lib.rs
crates/tobkiri-wasm/src/network.rs
crates/tobkiri-wasm/src/output.rs
crates/tobkiri-wasm/src/owned_values.rs
crates/tobkiri-wasm/src/sql_values.rs
crates/tobkiri-wasm/src/storage_values.rs
crates/tobkiri-wasm/src/values.rs
crates/tobkiri-wasm/src/wasi.rs
crates/tobkiri-wasm/src/wasi_effects.rs
crates/tobkiri-wasm/tests/component_structure.rs
crates/tobkiri-wasm/tests/limits.rs
crates/tobkiri-wasm/tests/port_proxy.rs
crates/tobkiri-wasm/tests/static_lifts.rs
crates/tobkiri-wasm/tests/typed_values.rs
crates/tobkiri-wasm/tests/warm_fiber.rs
crates/tobkiri-wit/Cargo.toml
crates/tobkiri-wit/src/abi.rs
crates/tobkiri-wit/src/lib.rs
crates/tobkiri-wit/wit/core-v1-1/core.wit
crates/tobkiri-wit/wit/core/core.wit
docs/migration/1520/astra-answers.md
docs/migration/1520/checkpoint-audit-20261015.md
docs/migration/1520/legacy-ledger.json
docs/migration/1520/merge-m0.md
docs/migration/1520/native-input-entry.md
docs/migration/1520/proc-next-slice.md
docs/migration/1520/resource-boundaries.md
docs/migration/1520/rust-contract.md
docs/migration/1520/rust-progress.md
docs/migration/1520/swe-data-moves-review-20261015.md
docs/migration/1520/swe-wit-review-20261015.md
migration/README.md
migration/fixtures/rumi_shell_policy_pack/inspection.json
migration/inventory.json
migration/inventory.py
migration/record_parity.py
migration/tests/test_inventory.py
packs/.gitignore
packs/Cargo.lock
packs/Cargo.toml
packs/README.md
packs/build.py
packs/guest-sdk/Cargo.toml
packs/guest-sdk/examples/float_vectors.rs
packs/guest-sdk/src/lib.rs
packs/guest-sdk/src/sse.rs
packs/local.echo-chat/flows/turn.flow.json
packs/local.echo-chat/pack.json
packs/local.echo-chat/wit/assemble-prompt.shape.json
packs/local.echo-chat/wit/assistant-output.shape.json
packs/local.echo-chat/wit/conversation-store.shape.json
packs/local.echo-chat/wit/echo-chat.wit
packs/local.echo-chat/wit/generate.shape.json
packs/local.echo-chat/wit/llm-provider.shape.json
packs/local.echo-chat/wit/load-context.shape.json
packs/local.echo-chat/wit/messages.shape.json
packs/local.echo-chat/wit/prompt-builder.shape.json
packs/local.echo-chat/wit/turn-flow.shape.json
packs/local.echo-chat/wit/user-input.shape.json
packs/local.echo-input/Cargo.toml
packs/local.echo-input/build.rs
packs/local.echo-input/config.json
packs/local.echo-input/pack.json
packs/local.echo-input/src/lib.rs
packs/local.echo-input/wit/pack.wit
packs/local.echo-llm/Cargo.toml
packs/local.echo-llm/build.rs
packs/local.echo-llm/config.json
packs/local.echo-llm/pack.json
packs/local.echo-llm/src/lib.rs
packs/local.echo-llm/wit/pack.wit
packs/local.echo-output/Cargo.toml
packs/local.echo-output/build.rs
packs/local.echo-output/config.json
packs/local.echo-output/pack.json
packs/local.echo-output/src/lib.rs
packs/local.echo-output/wit/pack.wit
packs/local.echo-prompt/Cargo.toml
packs/local.echo-prompt/build.rs
packs/local.echo-prompt/config.json
packs/local.echo-prompt/pack.json
packs/local.echo-prompt/src/lib.rs
packs/local.echo-prompt/wit/pack.wit
packs/local.echo-store/Cargo.toml
packs/local.echo-store/build.rs
packs/local.echo-store/config.json
packs/local.echo-store/pack.json
packs/local.echo-store/src/lib.rs
packs/local.echo-store/wit/pack.wit
packs/local.harness-agent/pack.json
packs/local.harness-agent/wit/harness-agent.wit
packs/local.harness-agent/wit/tool-call-guard.shape.json
packs/local.harness-agent/wit/tool-call.shape.json
packs/local.harness-agent/wit/tool-provider.shape.json
packs/local.harness-chat/flows/turn.flow.json
packs/local.harness-chat/pack.json
packs/local.harness-chat/wit/assemble-prompt.shape.json
packs/local.harness-chat/wit/assistant-output.shape.json
packs/local.harness-chat/wit/command-provider.shape.json
packs/local.harness-chat/wit/conversation-store.shape.json
packs/local.harness-chat/wit/generate.shape.json
packs/local.harness-chat/wit/harness-chat.wit
packs/local.harness-chat/wit/llm-provider.shape.json
packs/local.harness-chat/wit/load-context.shape.json
packs/local.harness-chat/wit/messages.shape.json
packs/local.harness-chat/wit/prompt-builder.shape.json
packs/local.harness-chat/wit/turn-flow.shape.json
packs/local.harness-chat/wit/user-input.shape.json
packs/local.harness-conversation-store/Cargo.toml
packs/local.harness-conversation-store/build.rs
packs/local.harness-conversation-store/config.json
packs/local.harness-conversation-store/pack.json
packs/local.harness-conversation-store/src/lib.rs
packs/local.harness-conversation-store/wit/pack.wit
packs/local.harness-prompt/Cargo.toml
packs/local.harness-prompt/build.rs
packs/local.harness-prompt/config.json
packs/local.harness-prompt/pack.json
packs/local.harness-prompt/src/lib.rs
packs/local.harness-prompt/wit/pack.wit
packs/local.harness-ui/pack.json
packs/local.harness-ui/ui/app.js
packs/local.harness-ui/ui/float.js
packs/local.harness-ui/ui/index.html
packs/local.harness-ui/ui/ports.js
packs/local.harness-ui/ui/style.css
packs/local.mock-llm/Cargo.toml
packs/local.mock-llm/build.rs
packs/local.mock-llm/config.json
packs/local.mock-llm/pack.json
packs/local.mock-llm/src/lib.rs
packs/local.mock-llm/wit/pack.wit
packs/local.openai-compat-provider/Cargo.toml
packs/local.openai-compat-provider/build.rs
packs/local.openai-compat-provider/config.json
packs/local.openai-compat-provider/pack.json
packs/local.openai-compat-provider/src/lib.rs
packs/local.openai-compat-provider/wit/pack.wit
packs/local.pi-adapter/COMPAT.md
packs/local.pi-adapter/Cargo.toml
packs/local.pi-adapter/build.rs
packs/local.pi-adapter/config.json
packs/local.pi-adapter/pack.json
packs/local.pi-adapter/src/lib.rs
packs/local.pi-adapter/wit/command-provider.shape.json
packs/local.pi-adapter/wit/pack.wit
packs/local.pi-adapter/wit/pi-adapter.wit
packs/local.pi-adapter/wit/pi-extension.shape.json
packs/local.pi-adapter/wit/tool-call-guard.shape.json
packs/local.pi-adapter/wit/tool-provider.shape.json
packs/local.pi-example-hello/config.json
packs/local.pi-example-hello/pack.json
packs/local.pi-example-hello/upstream.json
packs/local.pi-example-hello/upstream/hello.ts
packs/local.pi-example-hello/wit/pack.wit
packs/local.pi-example-hello/wrapper.js
packs/local.pi-example-hello/wrapper.js.map
packs/local.pi-example-protected-paths/config.json
packs/local.pi-example-protected-paths/pack.json
packs/local.pi-example-protected-paths/upstream.json
packs/local.pi-example-protected-paths/upstream/protected-paths.ts
packs/local.pi-example-protected-paths/wit/pack.wit
packs/local.pi-example-protected-paths/wrapper.js
packs/local.pi-example-protected-paths/wrapper.js.map
packs/local.preset-chatgpt-agent-deep-research-service/config.json
packs/local.preset-chatgpt-agent-deep-research-service/pack.json
packs/local.preset-chatgpt-agent-deep-research-service/wit/pack.wit
packs/local.preset-claude-code-service/config.json
packs/local.preset-claude-code-service/pack.json
packs/local.preset-claude-code-service/wit/pack.wit
packs/local.preset-cline-pair-programmer-service/config.json
packs/local.preset-cline-pair-programmer-service/pack.json
packs/local.preset-cline-pair-programmer-service/wit/pack.wit
packs/local.preset-gemini-cli-service/config.json
packs/local.preset-gemini-cli-service/pack.json
packs/local.preset-gemini-cli-service/wit/pack.wit
packs/local.preset-genspark-report-service/config.json
packs/local.preset-genspark-report-service/pack.json
packs/local.preset-genspark-report-service/wit/pack.wit
packs/local.preset-manus-project-service/config.json
packs/local.preset-manus-project-service/pack.json
packs/local.preset-manus-project-service/wit/pack.wit
packs/local.preset-openclaw-hermes-local-service/config.json
packs/local.preset-openclaw-hermes-local-service/pack.json
packs/local.preset-openclaw-hermes-local-service/wit/pack.wit
packs/local.shell-policy-python/config.json
packs/local.shell-policy-python/pack.json
packs/local.shell-policy-python/python-build.json
packs/local.shell-policy-python/python/app.py
packs/local.shell-policy-python/tests/inspection.json
packs/local.shell-policy-python/wit/pack.wit
packs/local.shell-policy-python/wit/policy.shape.json
packs/local.terminal-input/Cargo.toml
packs/local.terminal-input/build.rs
packs/local.terminal-input/config.json
packs/local.terminal-input/pack.json
packs/local.terminal-input/src/lib.rs
packs/local.terminal-input/wit/pack.wit
packs/local.terminal-output/Cargo.toml
packs/local.terminal-output/build.rs
packs/local.terminal-output/config.json
packs/local.terminal-output/pack.json
packs/local.terminal-output/src/lib.rs
packs/local.terminal-output/wit/pack.wit
packs/native-acceptance/.gitignore
packs/native-acceptance/README.md
packs/native-acceptance/provider/Cargo.toml
packs/native-acceptance/provider/src/lib.rs
packs/native-acceptance/provider/wit/deps/core.wit
packs/native-acceptance/provider/wit/pack.wit
packs/native-acceptance/provider/wit/review.shape.json
packs/native-acceptance/stage.mjs
packs/native-acceptance/ui/app.js
packs/native-acceptance/ui/float.js
packs/native-acceptance/ui/index.html
packs/native-acceptance/ui/pack.json
packs/native-acceptance/ui/ports.js
packs/native-acceptance/ui/style.css
packs/native-acceptance/verify-source.test.mjs
packs/profiles/echo.profile.json
packs/profiles/echo.published.json
packs/profiles/harness-live.profile.json
packs/profiles/harness-live.published.json
packs/profiles/harness-mock.profile.json
packs/profiles/harness-mock.published.json
packs/profiles/harness-ui.profile.json
packs/profiles/presets/chatgpt_agent_deep_research_service.published.json
packs/profiles/presets/chatgpt_agent_deep_research_service.published.provenance.json
packs/profiles/presets/claude_code_service.published.json
packs/profiles/presets/claude_code_service.published.provenance.json
packs/profiles/presets/cline_pair_programmer_service.published.json
packs/profiles/presets/cline_pair_programmer_service.published.provenance.json
packs/profiles/presets/gemini_cli_service.published.json
packs/profiles/presets/gemini_cli_service.published.provenance.json
packs/profiles/presets/genspark_report_service.published.json
packs/profiles/presets/genspark_report_service.published.provenance.json
packs/profiles/presets/manus_project_service.published.json
packs/profiles/presets/manus_project_service.published.provenance.json
packs/profiles/presets/openclaw_hermes_local_service.published.json
packs/profiles/presets/openclaw_hermes_local_service.published.provenance.json
packs/publish_presets.py
packs/shapes/echo-chat.wit
packs/shapes/harness-agent.wit
packs/shapes/harness-chat.wit
packs/shapes/pi-adapter.wit
packs/tests/test_input_entries.py
rust-toolchain.toml
sdk/js/.gitignore
sdk/js/pack-ui/float.js
sdk/js/pack-ui/index.js
sdk/js/package-lock.json
sdk/js/package.json
sdk/js/pi-shim/index.js
sdk/js/pi-shim/pi-ai.js
sdk/js/tests/adapter.test.mjs
sdk/js/tests/float-golden.json
sdk/js/tests/ports.test.mjs
sdk/js/wrap-pi.mjs
sdk/python/.gitignore
sdk/python/COMPOSITION.md
sdk/python/PATCH_PLAN.md
sdk/python/SQL.md
sdk/python/build_component.py
sdk/python/composition.py
sdk/python/deterministic_plan.py
sdk/python/tests/fixtures/build/basic/app.py
sdk/python/tests/fixtures/build/basic/pack.wit
sdk/python/tests/fixtures/build/basic/plan.json
sdk/python/tests/fixtures/build/shell-policy/app.py
sdk/python/tests/fixtures/build/shell-policy/oracle.json
sdk/python/tests/fixtures/build/shell-policy/plan.json
sdk/python/tests/fixtures/build/shell-policy/policy.py
sdk/python/tests/fixtures/build/shell-policy/tobkiri_sdk/__init__.py
sdk/python/tests/fixtures/build/shell-policy/tobkiri_sdk/_binding.py
sdk/python/tests/fixtures/build/shell-policy/tobkiri_sdk/audit.py
sdk/python/tests/fixtures/build/shell-policy/tobkiri_sdk/floats.py
sdk/python/tests/fixtures/build/shell-policy/tobkiri_sdk/generated.py
sdk/python/tests/fixtures/build/shell-policy/tobkiri_sdk/http.py
sdk/python/tests/fixtures/build/shell-policy/tobkiri_sdk/ports.py
sdk/python/tests/fixtures/build/shell-policy/tobkiri_sdk/proc.py
sdk/python/tests/fixtures/build/shell-policy/tobkiri_sdk/secrets.py
sdk/python/tests/fixtures/build/shell-policy/tobkiri_sdk/sql_codec.py
sdk/python/tests/fixtures/build/shell-policy/tobkiri_sdk/sql_result.py
sdk/python/tests/fixtures/build/shell-policy/tobkiri_sdk/storage.py
sdk/python/tests/fixtures/build/shell-policy/tobkiri_sdk/streams.py
sdk/python/tests/fixtures/build/shell-policy/wit/deps/core.wit
sdk/python/tests/fixtures/build/shell-policy/wit/pack.wit
sdk/python/tests/fixtures/outer-resources/inner.wat
sdk/python/tests/fixtures/outer-resources/pack.wit
sdk/python/tests/test_build_component.py
sdk/python/tests/test_composition.py
sdk/python/tests/test_dbapi.py
sdk/python/tests/test_deterministic_plan.py
sdk/python/tests/test_sdk.py
sdk/python/tests/test_sql_codec.py
sdk/python/tests/test_sql_result.py
sdk/python/tobkiri_sdk/__init__.py
sdk/python/tobkiri_sdk/_binding.py
sdk/python/tobkiri_sdk/audit.py
sdk/python/tobkiri_sdk/floats.py
sdk/python/tobkiri_sdk/generated.py
sdk/python/tobkiri_sdk/http.py
sdk/python/tobkiri_sdk/ports.py
sdk/python/tobkiri_sdk/proc.py
sdk/python/tobkiri_sdk/secrets.py
sdk/python/tobkiri_sdk/sql_codec.py
sdk/python/tobkiri_sdk/sql_result.py
sdk/python/tobkiri_sdk/storage.py
sdk/python/tobkiri_sdk/streams.py
sdk/python/toolchain-lock.json
sdk/python/tools/prove_outer_resources.py
tobkiri_launcher/.gitignore
tobkiri_launcher/core-host/Cargo.toml
tobkiri_launcher/core-host/README.md
tobkiri_launcher/core-host/src/isolation.rs
tobkiri_launcher/core-host/src/lib.rs
tobkiri_launcher/core-host/src/native_contract.rs
tobkiri_launcher/core-host/src/runtime.rs
tobkiri_launcher/core-host/src/transport.rs
tobkiri_launcher/frontend/core-host/index.html
tobkiri_launcher/frontend/package.json
tobkiri_launcher/frontend/src/core-host/boundedTransport.ts
tobkiri_launcher/frontend/src/core-host/config.test.ts
tobkiri_launcher/frontend/src/core-host/isolatedPackUi.test.ts
tobkiri_launcher/frontend/src/core-host/isolatedPackUi.ts
tobkiri_launcher/frontend/src/core-host/main.ts
tobkiri_launcher/frontend/vite.core-host.config.ts
tobkiri_launcher/src-tauri/Cargo.lock
tobkiri_launcher/src-tauri/Cargo.toml
tobkiri_launcher/src-tauri/build.rs
tobkiri_launcher/src-tauri/capabilities/core-host.json
tobkiri_launcher/src-tauri/permissions/autogenerated/core_host_output_ack.toml
tobkiri_launcher/src-tauri/permissions/autogenerated/core_start_profile.toml
tobkiri_launcher/src-tauri/permissions/autogenerated/core_stop_session.toml
tobkiri_launcher/src-tauri/permissions/autogenerated/core_terminal_reply.toml
tobkiri_launcher/src-tauri/permissions/autogenerated/pack_ui_ack.toml
tobkiri_launcher/src-tauri/permissions/autogenerated/pack_ui_cancel.toml
tobkiri_launcher/src-tauri/permissions/autogenerated/pack_ui_detach.toml
tobkiri_launcher/src-tauri/permissions/autogenerated/pack_ui_invoke.toml
tobkiri_launcher/src-tauri/permissions/autogenerated/pack_ui_register.toml
tobkiri_launcher/src-tauri/permissions/autogenerated/pack_ui_stream_cancel.toml
tobkiri_launcher/src-tauri/permissions/autogenerated/pack_ui_stream_pull.toml
tobkiri_launcher/src-tauri/src/core_host.rs
tobkiri_launcher/src-tauri/src/core_host_commands.rs
tobkiri_launcher/src-tauri/src/lib.rs
tobkiri_launcher/src-tauri/src/native_plan_review.rs
tobkiri_launcher/src-tauri/tauri.core-host.conf.json
tobkiri_runtime/docs/architecture.md
tobkiri_runtime/docs/capability_graph.md
tobkiri_runtime/docs/port_standards.md
tobkiri_runtime/docs/profile_spec.md
tools/legacy-vectors/COVERAGE.md
tools/legacy-vectors/freeze_reference.py
tools/legacy-vectors/freeze_wit_floats.py
tools/legacy-vectors/generate.py
tools/legacy-vectors/generate_extra.py
tools/legacy-vectors/launcher_source.rs
tools/python-producer/GAPS.md
tools/python-producer/README.md
tools/python-producer/TEST_PLAN.md
tools/python-producer/apply_patch.py
tools/python-producer/check_source.py
tools/python-producer/make_patch.py
tools/python-producer/overlay/componentize-py/producer-recipe-v2.json
tools/python-producer/overlay/componentize-py/runtime/src/tobkiri_preinit.rs
tools/python-producer/overlay/componentize-py/src/captured_inputs.rs
tools/python-producer/overlay/componentize-py/src/captured_wasi.rs
tools/python-producer/overlay/componentize-py/src/tobkiri_preinit.rs
tools/python-producer/overlay/componentize-py/staged_packaging.rs
tools/python-producer/overlay/componentize-py/tools/capture_snapshot.py
tools/python-producer/overlay/componentize-py/tools/stage_verify.py
tools/python-producer/overlay/cpython/Include/internal/pycore_tobkiri_entropy.h
tools/python-producer/overlay/cpython/Python/tobkiri_entropy.c
tools/python-producer/patches/componentize-py-0001.patch
tools/python-producer/patches/cpython-0001.patch
tools/python-producer/patches/wasmtime-wasi-0001.patch
tools/python-producer/pin_sdk.py
tools/python-producer/pins.json
tools/python-producer/sdk34-pins.json
tools/python-producer/source-delta.json
tools/python-producer/tests/test_source_patch.py
tools/quality/issue_1520_gate.py
tools/quality/test_issue_1520_gate.py
tools/wasm-sqlite/README.md
tools/wasm-sqlite/build-linux.py
tools/wasm-sqlite/build.py
tools/wasm-sqlite/host-toolchain-sdk34-x86_64-linux.json
tools/wasm-sqlite/inspect-linux-sdk.py
tools/wasm-sqlite/linux-sdk-34.proposed.json
tools/wasm-sqlite/locked-producer.json
tools/wasm-sqlite/private_sql.c
tools/wasm-sqlite/vendor/sqlite3.c
tools/wasm-sqlite/vendor/sqlite3.h
tools/wasmtime-allocation/README.md
tools/wasmtime-allocation/focused-review-set.json
tools/wasmtime-allocation/refresh_inventory.py
tools/wasmtime-allocation/source-inventory.json
tools/wasmtime-allocation/upstream-delta.patch
vectors/canon/LegacyFloat.jsonl
vectors/canon/LegacyPackSig.jsonl
vectors/canon/StrictV1.jsonl
vectors/canon/provenance.json
vectors/legacy-vectors.json
vectors/seals/approval-args.jsonl
vectors/seals/approval-token.jsonl
vectors/seals/artifact-tree.jsonl
vectors/seals/authority-audit.jsonl
vectors/seals/authority-lease.jsonl
vectors/seals/continuation-frame.jsonl
vectors/seals/debug-cli-operator.jsonl
vectors/seals/extra-provenance.json
vectors/seals/launcher-response.jsonl
vectors/seals/pack-ed25519-negative.jsonl
vectors/seals/pack-ed25519.jsonl
vectors/seals/packvm-bridge.jsonl
vectors/seals/packvm-deadline.jsonl
vectors/seals/release-nul.jsonl
vectors/seals/release-rust.jsonl
vectors/seals/sealed-environment.jsonl
vectors/seals/ui-operator.jsonl
vectors/seals/workflow-hmac.jsonl
vectors/wit-floats-provenance.json
vectors/wit-floats.json
vendor/.gitignore
vendor/wasmtime-internal-fiber/.cargo_vcs_info.json
vendor/wasmtime-internal-fiber/.gitignore
vendor/wasmtime-internal-fiber/Cargo.lock
vendor/wasmtime-internal-fiber/Cargo.toml
vendor/wasmtime-internal-fiber/Cargo.toml.orig
vendor/wasmtime-internal-fiber/LICENSE
vendor/wasmtime-internal-fiber/build.rs
vendor/wasmtime-internal-fiber/src/lib.rs
vendor/wasmtime-internal-fiber/src/miri.rs
vendor/wasmtime-internal-fiber/src/nostd.rs
vendor/wasmtime-internal-fiber/src/stackswitch.rs
vendor/wasmtime-internal-fiber/src/stackswitch/aarch64.rs
vendor/wasmtime-internal-fiber/src/stackswitch/arm.rs
vendor/wasmtime-internal-fiber/src/stackswitch/custom.rs
vendor/wasmtime-internal-fiber/src/stackswitch/loongarch64.rs
vendor/wasmtime-internal-fiber/src/stackswitch/riscv32imac.rs
vendor/wasmtime-internal-fiber/src/stackswitch/riscv64.rs
vendor/wasmtime-internal-fiber/src/stackswitch/s390x.rs
vendor/wasmtime-internal-fiber/src/stackswitch/x86.rs
vendor/wasmtime-internal-fiber/src/stackswitch/x86_64.rs
vendor/wasmtime-internal-fiber/src/unix.rs
vendor/wasmtime-internal-fiber/src/windows.c
vendor/wasmtime-internal-fiber/src/windows.rs
vendor/wasmtime/.cargo_vcs_info.json
vendor/wasmtime/Cargo.lock
vendor/wasmtime/Cargo.toml
vendor/wasmtime/Cargo.toml.orig
vendor/wasmtime/LICENSE
vendor/wasmtime/README.md
vendor/wasmtime/build.rs
vendor/wasmtime/proptest-regressions/runtime/vm/gc/enabled/free_list.txt
vendor/wasmtime/proptest-regressions/runtime/vm/instance/allocator/pooling/memory_pool.txt
vendor/wasmtime/src/compile.rs
vendor/wasmtime/src/compile/code_builder.rs
vendor/wasmtime/src/compile/code_builder/compile_time_builtins.rs
vendor/wasmtime/src/compile/runtime.rs
vendor/wasmtime/src/compile/stratify.rs
vendor/wasmtime/src/config.rs
vendor/wasmtime/src/engine.rs
vendor/wasmtime/src/engine/serialization.rs
vendor/wasmtime/src/lib.rs
vendor/wasmtime/src/profiling_agent.rs
vendor/wasmtime/src/profiling_agent/jitdump.rs
vendor/wasmtime/src/profiling_agent/perfmap.rs
vendor/wasmtime/src/profiling_agent/pulley.rs
vendor/wasmtime/src/profiling_agent/vtune.rs
vendor/wasmtime/src/runtime.rs
vendor/wasmtime/src/runtime/bug.rs
vendor/wasmtime/src/runtime/code.rs
vendor/wasmtime/src/runtime/code_memory.rs
vendor/wasmtime/src/runtime/component/allocation.rs
vendor/wasmtime/src/runtime/component/bindgen_examples/_0_hello_world.rs
vendor/wasmtime/src/runtime/component/bindgen_examples/_1_world_imports.rs
vendor/wasmtime/src/runtime/component/bindgen_examples/_2_world_exports.rs
vendor/wasmtime/src/runtime/component/bindgen_examples/_3_interface_imports.rs
vendor/wasmtime/src/runtime/component/bindgen_examples/_4_imported_resources.rs
vendor/wasmtime/src/runtime/component/bindgen_examples/_5_all_world_export_kinds.rs
vendor/wasmtime/src/runtime/component/bindgen_examples/_6_exported_resources.rs
vendor/wasmtime/src/runtime/component/bindgen_examples/_7_async.rs
vendor/wasmtime/src/runtime/component/bindgen_examples/_8_store_in_imports.rs
vendor/wasmtime/src/runtime/component/bindgen_examples/mod.rs
vendor/wasmtime/src/runtime/component/component.rs
vendor/wasmtime/src/runtime/component/concurrent.rs
vendor/wasmtime/src/runtime/component/concurrent/abort.rs
vendor/wasmtime/src/runtime/component/concurrent/error_contexts.rs
vendor/wasmtime/src/runtime/component/concurrent/func.rs
vendor/wasmtime/src/runtime/component/concurrent/future_stream_any.rs
vendor/wasmtime/src/runtime/component/concurrent/futures_and_streams.rs
vendor/wasmtime/src/runtime/component/concurrent/futures_and_streams/buffers.rs
vendor/wasmtime/src/runtime/component/concurrent/table.rs
vendor/wasmtime/src/runtime/component/concurrent/tls.rs
vendor/wasmtime/src/runtime/component/concurrent_disabled.rs
vendor/wasmtime/src/runtime/component/func.rs
vendor/wasmtime/src/runtime/component/func/host.rs
vendor/wasmtime/src/runtime/component/func/options.rs
vendor/wasmtime/src/runtime/component/func/typed.rs
vendor/wasmtime/src/runtime/component/has_data.rs
vendor/wasmtime/src/runtime/component/instance.rs
vendor/wasmtime/src/runtime/component/linker.rs
vendor/wasmtime/src/runtime/component/matching.rs
vendor/wasmtime/src/runtime/component/mod.rs
vendor/wasmtime/src/runtime/component/resource_table.rs
vendor/wasmtime/src/runtime/component/resources.rs
vendor/wasmtime/src/runtime/component/resources/any.rs
vendor/wasmtime/src/runtime/component/resources/host.rs
vendor/wasmtime/src/runtime/component/resources/host_dynamic.rs
vendor/wasmtime/src/runtime/component/resources/host_static.rs
vendor/wasmtime/src/runtime/component/resources/host_tables.rs
vendor/wasmtime/src/runtime/component/resources/ty.rs
vendor/wasmtime/src/runtime/component/storage.rs
vendor/wasmtime/src/runtime/component/store.rs
vendor/wasmtime/src/runtime/component/types.rs
vendor/wasmtime/src/runtime/component/values.rs
vendor/wasmtime/src/runtime/coredump.rs
vendor/wasmtime/src/runtime/debug.rs
vendor/wasmtime/src/runtime/exception.rs
vendor/wasmtime/src/runtime/externals.rs
vendor/wasmtime/src/runtime/externals/global.rs
vendor/wasmtime/src/runtime/externals/table.rs
vendor/wasmtime/src/runtime/externals/tag.rs
vendor/wasmtime/src/runtime/fiber.rs
vendor/wasmtime/src/runtime/func.rs
vendor/wasmtime/src/runtime/func/typed.rs
vendor/wasmtime/src/runtime/gc.rs
vendor/wasmtime/src/runtime/gc/disabled.rs
vendor/wasmtime/src/runtime/gc/disabled/anyref.rs
vendor/wasmtime/src/runtime/gc/disabled/arrayref.rs
vendor/wasmtime/src/runtime/gc/disabled/eqref.rs
vendor/wasmtime/src/runtime/gc/disabled/exnref.rs
vendor/wasmtime/src/runtime/gc/disabled/externref.rs
vendor/wasmtime/src/runtime/gc/disabled/i31.rs
vendor/wasmtime/src/runtime/gc/disabled/rooting.rs
vendor/wasmtime/src/runtime/gc/disabled/structref.rs
vendor/wasmtime/src/runtime/gc/enabled.rs
vendor/wasmtime/src/runtime/gc/enabled/anyref.rs
vendor/wasmtime/src/runtime/gc/enabled/arrayref.rs
vendor/wasmtime/src/runtime/gc/enabled/eqref.rs
vendor/wasmtime/src/runtime/gc/enabled/exnref.rs
vendor/wasmtime/src/runtime/gc/enabled/externref.rs
vendor/wasmtime/src/runtime/gc/enabled/i31.rs
vendor/wasmtime/src/runtime/gc/enabled/rooting.rs
vendor/wasmtime/src/runtime/gc/enabled/structref.rs
vendor/wasmtime/src/runtime/gc/noextern.rs
vendor/wasmtime/src/runtime/gc/none_ref.rs
vendor/wasmtime/src/runtime/instance.rs
vendor/wasmtime/src/runtime/instantiate.rs
vendor/wasmtime/src/runtime/limits.rs
vendor/wasmtime/src/runtime/linker.rs
vendor/wasmtime/src/runtime/memory.rs
vendor/wasmtime/src/runtime/module.rs
vendor/wasmtime/src/runtime/module/registry.rs
vendor/wasmtime/src/runtime/native_debug.rs
vendor/wasmtime/src/runtime/profiling.rs
vendor/wasmtime/src/runtime/resources.rs
vendor/wasmtime/src/runtime/stack.rs
vendor/wasmtime/src/runtime/store.rs
vendor/wasmtime/src/runtime/store/async_.rs
vendor/wasmtime/src/runtime/store/context.rs
vendor/wasmtime/src/runtime/store/data.rs
vendor/wasmtime/src/runtime/store/func_refs.rs
vendor/wasmtime/src/runtime/store/gc.rs
vendor/wasmtime/src/runtime/store/gc_disabled.rs
vendor/wasmtime/src/runtime/store/token.rs
vendor/wasmtime/src/runtime/trampoline.rs
vendor/wasmtime/src/runtime/trampoline/global.rs
vendor/wasmtime/src/runtime/trampoline/memory.rs
vendor/wasmtime/src/runtime/trampoline/table.rs
vendor/wasmtime/src/runtime/trampoline/tag.rs
vendor/wasmtime/src/runtime/trap.rs
vendor/wasmtime/src/runtime/try_mutex.rs
vendor/wasmtime/src/runtime/type_registry.rs
vendor/wasmtime/src/runtime/types.rs
vendor/wasmtime/src/runtime/types/matching.rs
vendor/wasmtime/src/runtime/unix.rs
vendor/wasmtime/src/runtime/v128.rs
vendor/wasmtime/src/runtime/values.rs
vendor/wasmtime/src/runtime/vm.rs
vendor/wasmtime/src/runtime/vm/always_mut.rs
vendor/wasmtime/src/runtime/vm/byte_count.rs
vendor/wasmtime/src/runtime/vm/component.rs
vendor/wasmtime/src/runtime/vm/component/handle_table.rs
vendor/wasmtime/src/runtime/vm/component/libcalls.rs
vendor/wasmtime/src/runtime/vm/component/resources.rs
vendor/wasmtime/src/runtime/vm/cow.rs
vendor/wasmtime/src/runtime/vm/cow_disabled.rs
vendor/wasmtime/src/runtime/vm/debug_builtins.rs
vendor/wasmtime/src/runtime/vm/export.rs
vendor/wasmtime/src/runtime/vm/gc.rs
vendor/wasmtime/src/runtime/vm/gc/data.rs
vendor/wasmtime/src/runtime/vm/gc/disabled.rs
vendor/wasmtime/src/runtime/vm/gc/enabled.rs
vendor/wasmtime/src/runtime/vm/gc/enabled/arrayref.rs
vendor/wasmtime/src/runtime/vm/gc/enabled/copying.rs
vendor/wasmtime/src/runtime/vm/gc/enabled/drc.rs
vendor/wasmtime/src/runtime/vm/gc/enabled/exnref.rs
vendor/wasmtime/src/runtime/vm/gc/enabled/externref.rs
vendor/wasmtime/src/runtime/vm/gc/enabled/free_list.rs
vendor/wasmtime/src/runtime/vm/gc/enabled/null.rs
vendor/wasmtime/src/runtime/vm/gc/enabled/structref.rs
vendor/wasmtime/src/runtime/vm/gc/enabled/trace_infos.rs
vendor/wasmtime/src/runtime/vm/gc/func_ref.rs
vendor/wasmtime/src/runtime/vm/gc/gc_ref.rs
vendor/wasmtime/src/runtime/vm/gc/gc_runtime.rs
vendor/wasmtime/src/runtime/vm/gc/host_data.rs
vendor/wasmtime/src/runtime/vm/gc/i31.rs
vendor/wasmtime/src/runtime/vm/helpers.c
vendor/wasmtime/src/runtime/vm/imports.rs
vendor/wasmtime/src/runtime/vm/instance.rs
vendor/wasmtime/src/runtime/vm/instance/allocator.rs
vendor/wasmtime/src/runtime/vm/instance/allocator/on_demand.rs
vendor/wasmtime/src/runtime/vm/instance/allocator/pooling.rs
vendor/wasmtime/src/runtime/vm/instance/allocator/pooling/decommit_queue.rs
vendor/wasmtime/src/runtime/vm/instance/allocator/pooling/gc_heap_pool.rs
vendor/wasmtime/src/runtime/vm/instance/allocator/pooling/generic_stack_pool.rs
vendor/wasmtime/src/runtime/vm/instance/allocator/pooling/index_allocator.rs
vendor/wasmtime/src/runtime/vm/instance/allocator/pooling/memory_pool.rs
vendor/wasmtime/src/runtime/vm/instance/allocator/pooling/metrics.rs
vendor/wasmtime/src/runtime/vm/instance/allocator/pooling/table_pool.rs
vendor/wasmtime/src/runtime/vm/instance/allocator/pooling/unix_stack_pool.rs
vendor/wasmtime/src/runtime/vm/interpreter.rs
vendor/wasmtime/src/runtime/vm/interpreter_disabled.rs
vendor/wasmtime/src/runtime/vm/libcalls.rs
vendor/wasmtime/src/runtime/vm/memory.rs
vendor/wasmtime/src/runtime/vm/memory/malloc.rs
vendor/wasmtime/src/runtime/vm/memory/mmap.rs
vendor/wasmtime/src/runtime/vm/memory/shared_memory.rs
vendor/wasmtime/src/runtime/vm/memory/shared_memory_disabled.rs
vendor/wasmtime/src/runtime/vm/memory/static_.rs
vendor/wasmtime/src/runtime/vm/mmap.rs
vendor/wasmtime/src/runtime/vm/mmap_vec.rs
vendor/wasmtime/src/runtime/vm/module_id.rs
vendor/wasmtime/src/runtime/vm/mpk/disabled.rs
vendor/wasmtime/src/runtime/vm/mpk/enabled.rs
vendor/wasmtime/src/runtime/vm/mpk/mod.rs
vendor/wasmtime/src/runtime/vm/mpk/pkru.rs
vendor/wasmtime/src/runtime/vm/mpk/sys.rs
vendor/wasmtime/src/runtime/vm/pagemap_disabled.rs
vendor/wasmtime/src/runtime/vm/parking_spot.rs
vendor/wasmtime/src/runtime/vm/provenance.rs
vendor/wasmtime/src/runtime/vm/send_sync_ptr.rs
vendor/wasmtime/src/runtime/vm/send_sync_unsafe_cell.rs
vendor/wasmtime/src/runtime/vm/stack_switching.rs
vendor/wasmtime/src/runtime/vm/stack_switching/stack.rs
vendor/wasmtime/src/runtime/vm/stack_switching/stack/dummy.rs
vendor/wasmtime/src/runtime/vm/stack_switching/stack/unix.rs
vendor/wasmtime/src/runtime/vm/stack_switching/stack/unix/x86_64.rs
vendor/wasmtime/src/runtime/vm/store_box.rs
vendor/wasmtime/src/runtime/vm/sys/custom/capi.rs
vendor/wasmtime/src/runtime/vm/sys/custom/mmap.rs
vendor/wasmtime/src/runtime/vm/sys/custom/mod.rs
vendor/wasmtime/src/runtime/vm/sys/custom/traphandlers.rs
vendor/wasmtime/src/runtime/vm/sys/custom/unwind.rs
vendor/wasmtime/src/runtime/vm/sys/custom/vm.rs
vendor/wasmtime/src/runtime/vm/sys/miri/mmap.rs
vendor/wasmtime/src/runtime/vm/sys/miri/mod.rs
vendor/wasmtime/src/runtime/vm/sys/miri/traphandlers.rs
vendor/wasmtime/src/runtime/vm/sys/miri/unwind.rs
vendor/wasmtime/src/runtime/vm/sys/miri/vm.rs
vendor/wasmtime/src/runtime/vm/sys/mod.rs
vendor/wasmtime/src/runtime/vm/sys/std_tls.rs
vendor/wasmtime/src/runtime/vm/sys/unix/machports.rs
vendor/wasmtime/src/runtime/vm/sys/unix/mmap.rs
vendor/wasmtime/src/runtime/vm/sys/unix/mod.rs
vendor/wasmtime/src/runtime/vm/sys/unix/pagemap.rs
vendor/wasmtime/src/runtime/vm/sys/unix/signals.rs
vendor/wasmtime/src/runtime/vm/sys/unix/traphandlers.rs
vendor/wasmtime/src/runtime/vm/sys/unix/unwind.rs
vendor/wasmtime/src/runtime/vm/sys/unix/vm.rs
vendor/wasmtime/src/runtime/vm/sys/windows/mmap.rs
vendor/wasmtime/src/runtime/vm/sys/windows/mod.rs
vendor/wasmtime/src/runtime/vm/sys/windows/traphandlers.rs
vendor/wasmtime/src/runtime/vm/sys/windows/unwind64.rs
vendor/wasmtime/src/runtime/vm/sys/windows/vectored_exceptions.rs
vendor/wasmtime/src/runtime/vm/sys/windows/vm.rs
vendor/wasmtime/src/runtime/vm/table.rs
vendor/wasmtime/src/runtime/vm/throw.rs
vendor/wasmtime/src/runtime/vm/traphandlers.rs
vendor/wasmtime/src/runtime/vm/traphandlers/backtrace.rs
vendor/wasmtime/src/runtime/vm/traphandlers/coredump_disabled.rs
vendor/wasmtime/src/runtime/vm/traphandlers/coredump_enabled.rs
vendor/wasmtime/src/runtime/vm/traphandlers/signals.rs
vendor/wasmtime/src/runtime/vm/vmcontext.rs
vendor/wasmtime/src/runtime/vm/vmcontext/vm_host_func_context.rs
vendor/wasmtime/src/runtime/wave.rs
vendor/wasmtime/src/runtime/wave/component.rs
vendor/wasmtime/src/runtime/wave/core.rs
vendor/wasmtime/src/runtime/windows.rs
vendor/wasmtime/src/sync_nostd.rs
vendor/wasmtime/src/sync_std.rs
vendor/wasmtime/tests/custom_signal_handler.rs
vendor/wasmtime/tests/engine_across_forks.rs
vendor/wasmtime/tests/host_segfault.rs
vendor/wasmtime/tests/pooling_alloc_near_oom.rs
vendor/wasmtime/tests/unload-engine.rs
```
<!-- staging-paths:end -->
