# Provider Registry translation candidate

This library translates the original Python Provider Registry's record
normalization, six state operations, Host request checks and redacted UI
projection. The original runtime remains authoritative and active. This crate
is not a registered production Pack and does not change Launcher, Setup,
approval, settings or chat routing.

`Registry<S, C>` uses explicit document-storage and clock ports. `MemoryStore`
retains ordered, decoded values and distinguishes an absent document from an
initialized store. It is a sequential test adapter, not an implementation of
the original filesystem lock, atomic write or Core transaction. A pure success
must not be used as proof that a native write was applied. Future Core storage
and authority failures must remain separate from source-level Python errors.

The model-access compiler is reused with its default `component` feature
disabled. The compiler's default component build remains enabled for its
existing consumers. The Registry library supplies no synthetic profile
authority, credential owner, network port, Core contribution or Wasm export.

Run its focused native checks from the repository root:

```sh
cargo test --manifest-path packs/Cargo.toml --locked \
  -p tobkiri-pack-provider-registry-rust
```

These checks do not establish original-source differential parity. The separate
observer in `tools/quality/python_provider_registry_compat` records the original
Python behavior; a full native and Wasm consumer is still required. Remaining
work includes raw JSON parser diagnostics and exact serialization effects,
transaction and contention behavior, actual Core Profile binding, Host
contribution authority, credential lifecycle, configuration integration and the
complete existing GUI flow.
