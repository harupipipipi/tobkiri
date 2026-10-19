# Isolated CPython 3.13 build trial

These scripts are exact executed stage commands, not a production installer.
They require the reviewed source at `/source`, SDK33 at `/sdk`, and a private
writable `/work` inside the separately controlled networkless sandbox. Do not
run them directly against arbitrary host paths. The receipt describes enforced
resource limits and observed results. No binary, secret, or full internal log
is included.

The native executable ran and reported CPython3.13.16 / Unicode15.1.0. At this initial
CPython-only checkpoint the WASI artifacts had only compiled/linked; export
inspection alone was not execution or ABI proof. Later stages are recorded below.
Optional native module absences, experimental dynamic linking and unchanged
SQLite restrictions remain explicit. This does not complete Issue1520.

## Runtime, assets and native producer

`producer-receipt.json` records the later successful sync and async runtime
builds, packaging of all 13 real assets, and native producer compilation.
`wasi-runtimes.sh` and `producer-cli.sh` are the exact successful stage scripts;
`package-assets.sh` reproduces the exact packaging invocation. These are stage
commands, not sandbox launchers: the caller must enforce the recorded fresh
output, read-only input, cleared environment, network and resource constraints.

Runtime builds use the unchanged upstream lock, Rust 1.94.0, SDK33 and the
previously built CPython shared library. The sole Git dependency is the reviewed
`dicej/wasm-tools` commit recorded in the receipt. No normal top-level build
script or automatic native dependency download ran. The first runtime attempt
failed because `cc` resolved through an unavailable alternatives link; the
successful fresh retry selected the existing GCC/G++ explicitly.

The packaging input manifest binds 11 binaries, 686 standard-library files and
six bundled files. The standalone helper preserves the reviewed Wasm custom
section whitelist. Its deliberate tar ordering/metadata normalization is not a
claim of byte-identical upstream packaging. Independent decoding checked all
13 outputs, all retained Wasm sections and every tar member; the real sync and
async runtime outputs remained distinct.

The native CLI source copy differs only by adding `build = "alt-build.rs"` in
its package manifest and the 13 packaged assets. Its lock and all other original
sources are unchanged. All 142 source/assets files were rechecked after the
build. Actual `--version` and `--help` calls succeeded in the isolated sandbox;
this proves executable startup, not the version or behavior of a running guest.

At the producer-only checkpoint, componentization, Python preinitialization,
guest runtime identity, typed 33-case replay and native Core acceptance were
separate pending gates. The later component receipt below advances some of them.
The custom trial does not change the SDK's official-wheel verifier or enable a
production default.

For the runtime stage, `/work/package/pyo3-config.txt` is the reviewed staged
`pyo3-config.txt` with only `lib_dir=/work/wasi` appended. It and the existing
WASI build directory are mounted read-only. `/rust` is the pinned native Rust
1.94.0 toolchain; the dedicated Cargo home's registry and Git trees are also
read-only after complete archive/source-closure verification. For packaging,
`/inputs` contains only the manifest-bound runtime, CPython, SDK, adapter,
standard-library and bundled inputs; `/helper` is the reviewed standalone
packager. The native CLI stage mounts the separately verified source/assets
copy at `/source/componentize-py` and does not inherit the runtime stage's flags.

## Actual component and development replay

The later `component-receipt.json` records successful componentization and
Python preinitialization, exact outer-component ABI validation, and actual
export calls. `componentize-trial.py` and `replay-trial.py` are the exact drivers
used inside the separately enforced sandbox, not standalone sandbox launchers.
The custom driver reuses source-plan and composition checks without replacing
or weakening the SDK's official-wheel verifier. Its binary and plan hashes are
explicit trial pins, not a new production default.

The running guest reported Python 3.13.16 / Unicode 15.1.0. Of 33 development
cases, 12 success strings and 12 typed-invalid codes matched; nine documented
profile exclusions were rejected. This does not establish error-message parity
or full original integer/nonfinite/depth compatibility. Each complete case
shares its fuel and timeout budget across instantiation, lifecycle init,
runtime-info and normalization. Lifecycle shutdown was not covered here.

The replay's minimal host imports trap if called. It is not native Core/Gate,
provider dispatch or consumer-permission evidence. The actual hash-bound Pack
was materialized, but production enablement and native Core acceptance remain
false in this dated receipt. Resident memory and process-count monitoring are
sampled safeguards, not hard RSS/PID caps or generic hostile-process guarantees.
