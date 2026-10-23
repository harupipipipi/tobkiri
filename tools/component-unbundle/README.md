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

CI uses the two authenticated original Python components and retains only the
JSON measurement. The full original native replay remains independent and
unchanged. The report explicitly leaves guest execution, Host admission and
production adoption false. It cannot establish five-Pack admission, ABI/runtime
parity, or shared-source loading. Those require authenticated frozen module
dependencies, original Pack approval binding, source-stage bounded sharing,
funded compilation and cache lifetimes, and independent Store/memory/table/
global/WASI/CallCx state for every Pack. The Host's existing import rejection
and 64 MiB catalog guard remain intact.
