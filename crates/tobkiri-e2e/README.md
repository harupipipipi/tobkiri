# Component acceptance tests

The acceptance recipe uses actual Rust Wasm components. CI is configured to
build and test them; a passing current workflow or complete migration is not yet
established. The current contract is
[resource-boundaries.md](../../docs/migration/1520/resource-boundaries.md).

From the repository root, after the reviewed Rust 1.94.0 toolchain and
`wasm32-wasip2` target are installed:

```sh
export CARGO_TARGET_DIR="$PWD/../.work/cargo-target"
export CARGO_BUILD_JOBS=2
export CARGO_INCREMENTAL=0
python3 -B packs/build.py --target-dir "$CARGO_TARGET_DIR"
cargo test -p tobkiri-e2e --locked --test chat --test network --test ui
```

`packs/build.py` runs the separate guest workspace with its committed lock:
`cargo build --manifest-path packs/Cargo.toml --locked --target wasm32-wasip2
--workspace`. It stages actual component bytes, records provenance, binds config
and WIT by hash, and refreshes manifest/Profile pins. The builder refuses to run
below the agreed 5 GiB disk floor. Heavy build and replay work uses one measured
capacity lease at a time.

Tracked inputs include sources, WIT, workspace locks, manifests, Profiles, SDKs
and frozen fixtures. Pack `component.wasm`, Rust/JS `component.build.json`, staged
`wit/deps`, and native build environments are generated and ignored. The tests
copy admitted files into a private fixture and verify their bytes. The guest
workspace builds twelve Rust components. Input and output are distinct Packs,
so an input stream pull does not reenter its own guest during Flow output.

`chat` covers the Harness Wasm mock, a separate Echo Pack set without Harness,
stream ordering, a frozen plan digest and conversation storage across a Core
restart. `network` uses a test-only external SSE server while the turn traverses
input Pack → Flow → Wasm provider → streamed output → Core SQLite. `ui` exercises
the Core-owned stream facade with actual input/Flow/provider/store components,
foreign-token rejection, bounded pull and detach. It does not prove browser
rendering or native WebView integration.

## Current checkpoint, 2026-10-15

Private receipts under the parent workspace's `.work/evidence/` record:

| Check | Result and limit |
| --- | --- |
| `snapshot-generation3-product-check4-20261015.json` | Offline locked `cargo check` passed for Core, CLI and Launcher core host, with identical source hashes before/after. This is compilation evidence. |
| `snapshot-e2e-xhigh-check-20261015.json` | Offline locked E2E `--tests` check passed, with identical source hashes before/after. Test bodies were not executed by this check. |
| `snapshot-native-vfs-custody-tests-20261015.json` | Fourteen store library tests passed, including actual SQLite quota rollback/reopen and VFS custody cases. Subsequent F5 quota/custody changes remain unrun. |
| `python-producer-sol-xhigh-20261015.json` | All 54 Python SDK tests passed; installed tool RECORD consistency and launcher selection passed. No component compiler or Rust replay ran in this checkpoint. |
| `snapshot-python-current-parity1-20261015.json` | Actual Python Pack replay through Rust Core failed before the first frozen case with `invalid Core control reservation`. Zero-Flow catalog accounting is under repair; no 91-case success is claimed. |

Native guest DATA uses the approved bounded native SQLite backend. It retains
Profile/Pack/tier isolation, Gate/approval checks, SQL limits, cancellation,
transaction custody and grouped DB/WAL/journal/SHM/temp quotas. The private
SQLite-in-Wasm producer is an optional experiment. Current DATA acceptance still
requires replay after the latest changes and the full private/public/readonly,
persistence and cancellation paths.

## Historical M2 and live model evidence

[evidence/m2-final.json](evidence/m2-final.json) records four deterministic tests
and one real model test, all passed, with commands, artifact/model identities
and source identity before/after. It predates strict explicit User/Pack policy,
SQL 1.1, allocation/control accounting, lifecycle and durable outbox changes.
It proves its recorded source snapshot, not the current workspace.

Capture a fresh checkpoint with `python3 -B crates/tobkiri-e2e/tools/capture_m2.py`
after its prerequisites are satisfied. Raw logs and the test Profile identity
stay in private `.work` evidence. A new receipt must exercise the actual current
bounded DATA and authority paths.

The real server test is opt-in:

```sh
TOBKIRI_TEST_LLAMA=1 \
TOBKIRI_TEST_LLAMA_KEY_FILE="$PWD/../.work/llama/api-key.txt" \
cargo test -p tobkiri-e2e --locked --test llama -- --ignored --nocapture
```

The fixture targets `gemma3-1b-local` at the declared origin
`http://127.0.0.1:18080`. It imports a mode-0600 key file through the generic Core
secret operation, never echoes it, and scans the data root/export for plaintext.
It requires five deltas, ordered Flow events, a save step and cancellation after
a delta, then probes server slot state. Each fetch must record two opaque secret
dispatches, each with one terminal grant outcome. A fresh current-source run
through Harness, provider and the local API remains required.

## Python and Pi parity

These components have separate authoring and parity acceptance. They are not
rebuilt for the M2 Rust component recipe. Python uses a development environment
with pinned official `componentize-py==0.25.0`, `wasmtime==48.0.0` and WAC 0.12.0.
The producer captures reviewed source/WIT/bindings/tool/options identities,
verifies exact imports/exports and composes the private initializer behind a
typed outer component. Native Python and the compiler are build tools; product
hosts run verified components in Rust without invoking them.

The official shell-policy build produced a 21,480,859-byte outer component in
`python-official-current/shell-policy.build.json`; its build receipt is
`python-official-shell-policy-build2-20261015.json`. That artifact predates the
latest producer hardening, which needs a new build. The receipt honestly records
`archive_sha256_verified: false`; installed RECORD consistency does not
authenticate an installed tree against an archived wheel.

Custom CPython/compiler patches and identical bytes across compiler builds are
optional hardening and reproducibility work. They do not block the ordinary
verified-wheel route. Canonical/seal/digest compatibility remains required, and
runtime cryptographic entropy must use the gated random source. Historical
resource tests prove own/borrow/drop, cross-interface nominal identity,
foreign-handle rejection and private-initializer hiding for their fixture; they
do not replace current Pack/Core acceptance.

Pi compilation uses `npm ci --prefix sdk/js` and pinned build-time
esbuild/componentize-js tools. Neither Python nor Node ships as a Pack runtime.
After separately building and admitting the components, replay is explicit:

```sh
cargo test -p tobkiri-e2e --locked --test parity --test pi -- --ignored --nocapture
```

The Python test also accepts an explicitly verified staged Pack through
`TOBKIRI_TEST_PYTHON_PACK_DIR`. Its original 91 shell-classifier/tokenizer cases,
ordering and pins remain unchanged. The original private-initializer/WIT failure
is preserved in [evidence/m4-pending.json](evidence/m4-pending.json).
[evidence/python-outer-producer-pending-002.json](evidence/python-outer-producer-pending-002.json)
and the Python source-preparation receipts remain historical prototype evidence;
their earlier mandatory reproducibility assumptions are superseded by the
current resource-boundary decision.

Two actual Pi tests previously passed the unchanged hello result and five
protected-path decisions through wrappers, adapter and Gate.
[evidence/pi-producer-runtime-002.json](evidence/pi-producer-runtime-002.json)
predates strict User policy and needs current replay. Full Harness tool-loop and
protected-file effects, three legacy mediator conversions and complete DB-API
parity remain open. SDK codec/routing tests do not establish actual storage
parity. The 837 migration inventory units remain unresolved; these checkpoints
do not authorize removal of the legacy runtime.
