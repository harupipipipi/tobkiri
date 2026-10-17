# DeepThink v5 execution and build scope

DeepThink v5 completes one workflow phase per invocation. The phase result,
validated generation and paused state are saved before the next phase can start.
A `paused` event is a continuation receipt, not a completed assistant answer.
The existing `local.deepthink.continuation-input@1.0` User port resumes the same
run under a new ordinary request. In-flight transports retain their original
deadline; the change does not extend the 300-second request limit.

The conversation store skips paused segments. When a continuation finally
produces an approved answer, it records the original task and accepted
clarifications with that answer. The UI enables continuation only for its saved
paused state and matching conversation history. It does not infer approval from
Flow completion or restore an unverified candidate as an answer.

## Reproduce the optimized Wasm staging

From the repository root, use the pinned Rust toolchain and locked dependencies:

```powershell
cargo build --manifest-path packs/Cargo.toml --workspace --release --target wasm32-wasip2 --locked
python -B packs/build.py --skip-build --build-profile release --target-dir target
python -B packs/build_deepthink_profile.py --build-profile release --target-dir target
cargo build -p tobkiri-cli --release --locked
```

Supply the same explicit Cargo target directory to both staging commands if
`CARGO_TARGET_DIR` is set. The baseline staging must run first so provider,
conversation and prompt variants copy components and provenance from the same
build profile. There is no fallback from a missing release component to debug.
`component.build.json` records the selected profile and actual artifact/source
hashes. The authored DeepThink UI is preserved and rebound into its Pack.

Normal release CLI uses the platform DATA directory. An isolated acceptance
fixture can retain the existing test DATA API while optimizing dependencies:

```powershell
cargo build -p tobkiri-cli --release --locked --config profile.release.package.tobkiri-core.debug-assertions=true --config profile.release.package.tobkiri-cli.debug-assertions=true
```

This is an optimized **test build**, not a certification of normal release
configuration. It changes no source guard, Gate approval, quota, owner proof or
audit validation. The production release rejection of `--data-root` remains.
Do not move or reset an existing DATA directory to work around that rejection.

## Remaining limits

A nonterminal generation, unknown checkpoint/tool outcome or missing finalized
generation blocks continuation. v4 state is preserved, and matching old inputs
cannot silently create a v5 replacement. The current code does not reconcile a
remote generation whose complete public answer was lost. A phase that itself
exceeds its request deadline can therefore still require reconciliation.

The provider output-token parameter remains omitted. Public parsing permits
4 MiB, while a single Core KV value permits 1 MiB; chunked persistence is still
unfinished. A dedicated read-only User inspector, live parallel inspection,
all-host activation and parity with the legacy 154 tools remain unfinished.
Unit/ABI/staging results and historical tool receipts must be distinguished from
actual execution of the current release Pack hashes. Terminal-Bench acceptance
has not been established by these changes.
