set -eu
export PATH=/rust/bin:/usr/bin:/bin
export RUSTC=/rust/bin/rustc
export CARGO_TARGET_X86_64_UNKNOWN_LINUX_GNU_LINKER=/usr/bin/gcc
export CC=/usr/bin/gcc CXX=/usr/bin/g++
export CARGO_HOME=/work/runtime-cargo-home
export CARGO_BUILD_JOBS=1 CARGO_INCREMENTAL=0 CARGO_PROFILE_RELEASE_DEBUG=0
export CARGO_NET_OFFLINE=true
export PYO3_CONFIG_FILE=/work/package/pyo3-config.txt
export CARGO_TARGET_WASM32_WASIP2_LINKER=/sdk/bin/clang
export RUSTFLAGS='--cfg pyo3_disable_reference_pool -Clink-args=-Wl,--skip-wit-component -Clink-args=-shared -Clink-args=-L/work/wasi -Clink-args=-lpython3.13 -Clink-self-contained=n'
cd /source/componentize-py
CARGO_TARGET_DIR=/work/runtime-sync /rust/bin/cargo build --frozen -j1 -p componentize-py-runtime --target=wasm32-wasip2 --release
CARGO_TARGET_DIR=/work/runtime-async /rust/bin/cargo build --frozen -j1 -p componentize-py-runtime --target=wasm32-wasip2 --release --features=async
