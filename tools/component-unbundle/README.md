# Component module extraction

Build-time tool for preserving the original Python Pack implementations while
removing duplicated embedded core-module bytes. It does not admit a Pack, grant
authority, execute a guest, or enable a replacement product runtime.

The type conversion and index-rewriting helpers derive from Bytecode Alliance
[wasm-tools v1.245.1](https://github.com/bytecodealliance/wasm-tools/blob/v1.245.1/src/bin/wasm-tools/component.rs).
`upstream-source.json` pins the complete source digest. The helper algorithms
are preserved, with rustfmt formatting; the upstream filesystem-writing driver
is replaced by bounded stdin/stdout handling. Apache 2.0 with LLVM exception
attribution and license are retained. No new dependency versions are introduced:
the 18-package locked closure is a reachable subset of the reviewed Python 3.13
asset packager registry closure.

The worker fully validates input function bodies, collects original embedded
core modules in traversal order, extracts modules larger than 10 KiB, derives
their import/export types, and replaces definitions with aliases to new typed
module imports. Extracted module bytes are borrowed unchanged. It validates the
resulting component before emitting output. Unsupported type conversions or
preexisting `unbundled-module*` root imports fail closed. The existing 32 MiB
per-input bound remains, alongside 16 nested components, 16,384 component
sections, 4096 modules, and a 64 MiB output-envelope bound.

```
cargo test --locked --manifest-path tools/component-unbundle/Cargo.toml
cargo build --locked --manifest-path tools/component-unbundle/Cargo.toml
python3 -B tools/component-unbundle/test_measure.py
python3 -B tools/component-unbundle/measure.py --worker /absolute/trusted/worker \
  /verified/caller/component.wasm /verified/bridge/component.wasm
```

The worker receives no arguments and writes `TBUNB001`, little-endian u32 module
count and component length, component bytes, then each module's little-endian
u32 length and exact bytes. Import names are `unbundled-module0`, etc. The
measurement driver runs one explicitly trusted local worker at a time with a
120-second deadline. It streams output through a temporary file, verifies
source identity and content around extraction, rejects unexpected bytes or
module order, and hashes every extracted module against the original embedded
module inventory. Its JSON reports the actual wrapper-plus-unique-module size.
Worker and source hashes establish observed identity, not independent trust.

`materialize.py` uses the same extractor to publish exact code under a fresh,
absolute destination in a trusted parent directory. It verifies the original
embedded bytes, per-Pack 32 MiB dependency closure, the 64 MiB catalog closure,
4096 total original modules and at most 256 extracted bindings per Pack. Failed
partials are private and removed; atomic no-replace publication preserves any
existing destination. The receipt still records no guest execution, Host
admission or production activation.

```
python3 -B tools/component-unbundle/materialize.py --worker /absolute/trusted/worker \
  --output /trusted/fresh-bundle \
  /verified/bridge/component.wasm /verified/caller/component.wasm
```

CI separately consumes this bundle for the exact original Python bridge/caller
graph. The E2E adapter checks the pinned extraction receipt and original source
digests, keeps every original noncomponent file and manifest field, and adds
the extracted dependency inventory and `runtime.core_modules` mapping. These
changes produce new Pack hashes and Profile pins; Core admits them through its
ordinary verifier, Gate, Audit and Wasmtime type checks. The same original
30-case comparison, denied initialization and shutdown checks run through that
graph. The unchanged original replay runs separately, and its success cannot
satisfy the transformed replay. CI retains compact receipts and logs, not the
extracted binaries.

The new replay is an additional two-Pack native test, not five-Pack acceptance,
Launcher GUI approval, production activation or VM retirement. Only its actual
runtime receipt can establish transformed execution. Explicit authenticated
module bindings do not relax the remaining import rejection, input bounds, or
independent Store/memory/table/global/WASI/CallCx state for every Pack.
