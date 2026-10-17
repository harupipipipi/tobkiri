# DeepThink development Packs

DeepThink adds a resumable workflow to the Rust/Wasm Harness introduced in
PR #1521. It is an ordinary Pack overlay, selected by Profile bindings. User
input and continuation enter Core Flow; the product accesses models, tools and
persistence through typed ports and Gate.

## Workflow

The workflow first turns the request into a Goal. It defines each abstract
quality term separately, then establishes acceptance criteria before preflight,
planning, capability assessment, tool/skill selection, working environment,
Profile checks, references, evidence, individual sections, integration and
review. Each step produces a short public decision summary. Private reasoning
is neither requested nor recorded.

The Goal and checkpoint are logical artifacts in the Pack's private Core store,
including `goal.json`, `goal.md`, drafts, accepted answers and quarantined
candidates. These names do not grant access to arbitrary host files.

Continuation uses `local.deepthink.continuation-input@1.0` and its dedicated
Flow. It retains the original Goal, completed generations, tool receipts and
usage. Clarification answers re-evaluate the stopped phase. A failed generation
can be retried without discarding earlier work. A tool with an unknown outcome
is not automatically replayed; the workflow requires reconciliation. Review
limits still apply when resuming.

## OpenRouter and usage

Build the existing components and then compose the DeepThink Profiles:

```sh
cargo test --manifest-path packs/Cargo.toml -p tobkiri-guest-sdk -p tobkiri-pack-harness-tools -p tobkiri-pack-deepthink
cargo build --manifest-path packs/Cargo.toml --workspace --locked --target wasm32-wasip2 --target-dir <target>
python3 -B packs/build.py --skip-build --target-dir <target>
python3 -B packs/build_deepthink_profile.py --target-dir <target>
node --test packs/tests/test_ui_accounting.mjs
```

The build uses Rust 1.94.0, `wasm32-wasip2`, and build-time Python with PyYAML.
It creates `deepthink-mimo.profile.json`, its UI variant and DeepThink variants
of all seven existing style presets. Published documents and provenance are
written beside them. Existing local/mock Profiles keep their own providers.

The new provider uses `https://openrouter.ai/api/v1` and
`xiaomi/mimo-v2.6-flash`. Its only credential reference is the Profile-scoped
`api-key`; configure it through Core's secret API after creating and approving
the Profile on a supported host. Credentials are not part of Pack config or
published Profiles.

`max_tokens: 0` means omit the output token parameter from the provider request.
Positive explicit limits are preserved. A successful stream requires an
explicit normal finish and `[DONE]`; a disconnect is not a successful answer.
Provider-reported input, output, cached, reasoning and total tokens, plus cost,
are retained and displayed. Missing receipts remain unknown, and partial totals
identify unconfirmed attempts rather than reporting their usage as zero.

## Tools

`local.harness-tools` provides nine operations:

- `artifact.write/read/stat/list/delete`: private UTF-8 text artifacts, SHA-256,
  metadata pagination and deletion tombstones. Text is limited to 256 KiB.
- `calculator`: exact integer add/subtract/multiply/divide within
  +/-9,007,199,254,740,991. Division must have no remainder.
- `json.validate`: JSON syntax, type and error location.
- `clock.now`: Core clock in Unix milliseconds.
- `random.bytes`: 1 through 256 bytes from Core randomness.

Artifact mutation and its receipt are stored atomically in one SQL row. Stable
call IDs prevent replay from reapplying an earlier mutation; reuse with
different arguments is rejected. Artifact paths are relative logical names,
not host filesystem paths. Historical content and receipts remain after delete.

These are new tools. The legacy Defaults tool catalog, terminal, browser,
Computer use and 3D editing software are not migrated by these Packs.

## Verification and current platform limits

The Windows verification run passed 221 host pure tests and 35 guest tests
(SDK 10, tools 7, DeepThink 18). A separate explicitly selected Rust test verified
30 staged Wasm components against production WIT/import/ABI checks, compiled
20 Profiles and rejected 20 modified pins. That is 257 Rust test cases in total;
the component and Profile subchecks are not added again. All 35 `.tpk` archives
passed CLI build/verify and source/archive manifest hash comparison. Three UI
usage-formatting tests and five isolated SQLite SQL fixtures also passed.

These checks do not establish a successful Core session. In the PR #1521
baseline, Windows Core stops before Profile startup with:

```text
descriptor-relative nofollow storage is unsupported on this platform
```

Secure DataRoot, allocation-owner proof, capability/quota SQLite VFS and other
platform effects need Windows implementations. The existing Unix owner proof
also restricts support to Apple Silicon, local APFS and Darwin 24.6.0; moving to
WSL does not satisfy it. The platform checks remain enforced. A separate Python
Lab's live model result is not evidence of running this Rust Core.

The full host test target also has 36 pre-existing old-API compile errors in
`crates/tobkiri-wasm/tests/port_proxy.rs`. Legacy migration fixtures still retain
historical counts/digests. All legacy tools and Defaults host execution remain
pending migration and runtime verification. Terminal-Bench has not been run.
