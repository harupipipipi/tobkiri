# Isolated CPython 3.13 build trial

These scripts are exact executed stage commands, not a production installer.
They require the reviewed source at `/source`, SDK33 at `/sdk`, and a private
writable `/work` inside the separately controlled networkless sandbox. Do not
run them directly against arbitrary host paths. The receipt describes enforced
resource limits and observed results. No binary, secret, or full internal log
is included.

The native executable ran and reported CPython3.13.16 / Unicode15.1.0. The WASI
artifacts only compiled/linked; export inspection is not execution or ABI proof.
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

Componentization, Python preinitialization, guest runtime identity, typed
33-case replay and native Core acceptance are still separate pending gates.
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
