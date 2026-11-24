set -eu
export PATH=/rust/bin:/usr/bin:/bin
export RUSTC=/rust/bin/rustc
export CARGO_TARGET_X86_64_UNKNOWN_LINUX_GNU_LINKER=/usr/bin/gcc
export CC=/usr/bin/gcc CXX=/usr/bin/g++
export CARGO_HOME=/work/cargo-home CARGO_TARGET_DIR=/work/target
export CARGO_BUILD_JOBS=1 CARGO_INCREMENTAL=0 CARGO_PROFILE_RELEASE_DEBUG=0
export CARGO_NET_OFFLINE=true
export COMPONENTIZE_PY_TEST_SEED=0000000000000000000000000000000000000000000000000000000000000000
cd /source/componentize-py
/rust/bin/cargo build --frozen -j1 --release -p componentize-py --bin componentize-py
