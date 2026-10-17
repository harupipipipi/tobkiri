These isolated Rust components exercise the host without any application Pack.
The package names and operations are fixtures only. The consumer really imports
the provider's typed arithmetic interface; the host proxy supplies that import.

Build with Rust 1.94.0 and wasm32-wasip2, using the shared target directory:

```sh
CARGO_TARGET_DIR=/absolute/workspace/.work/cargo-target CARGO_BUILD_JOBS=2 \
  CARGO_INCREMENTAL=0 cargo build --manifest-path crates/test-guests/Cargo.toml \
  --target wasm32-wasip2 --locked
```

The test workspace has a separate lock file and is excluded from the product
workspace. The core WIT dependency is copied from the owned authoritative source
by the build preparation step; its contents must stay byte-identical.
