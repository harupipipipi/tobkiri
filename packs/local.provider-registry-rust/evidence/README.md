# Original Provider Registry observations

`original-python/` contains the unchanged synthetic observations produced by
`tools/quality/python_provider_registry_compat/generate.py` with CPython 3.13.9.
The receipt SHA-256 is
`e87762d4647d05ad8800c337c2352a272c767d252cb7ad4c4906760fc5590b37`.
Its 221 input/result/state sidecars are authenticated before replay, together
with the receipt's complete 346-file original source closure. The fixture
contains no user Profile, API keys, private conversation or copied source tree.

The thirty source cases define 86 actions. The source executed 85; one action
was not dispatched after its expected constructor failure. The decoded Rust
consumer executes eighty calls and compares 81 outcomes, including that
constructor. Five recorded actions remain explicitly unverified. Nine calls
exercise only the captured-operation helper and one exercises only a capture
predicate; none recreates the opaque Host capture object.

The native adapter stores decoded documents in memory. This test does not prove
raw JSON parser behavior, exact file bytes, file permissions, locking, atomic
replacement, concurrency, real credential effects, Core/Wasm integration or
production activation. Its summary keeps
`complete_provider_registry_parity_passed` false.

Source-generated timestamps may differ between clocks. Seeded timestamps must
remain exact, and returned generated timestamps must match the same side's
stored record and clock use. Independent regression tests reject missing or
corrupted returned timestamps; the consumer does not remove every timestamp
before comparing values.

The `Decoded Registry original-source comparison` CI step explicitly runs the
otherwise ignored consumer and retains its private output journal and summary.
For a local run, set `TOBKIRI_PROVIDER_REGISTRY_ORACLE_RECEIPT` to the absolute
path of `original-python/receipt.json`,
`TOBKIRI_PROVIDER_REGISTRY_ORACLE_SHA256` to the hash above, and
`TOBKIRI_PROVIDER_REGISTRY_EVIDENCE_DIR` to a new directory whose parent exists.
Then run:

```sh
cargo test --locked --manifest-path packs/Cargo.toml \
  -p tobkiri-pack-provider-registry-rust --lib \
  oracle_tests::provider_registry_decoded_source_consumer -- --ignored --exact
```

Missing inputs, changed hashes, unknown actions and existing evidence output
directories fail the explicit consumer. A future source change requires new
original-source observations and reviewed pins; Rust output must not replace
the original expectations.

The current receipt was regenerated from the unchanged original generator on
2026-10-25 after the captured-input platform artifact refactor. All thirty
inputs are byte-identical; generated timestamps and their authenticated state
sidecars reflect the new observation clock. The prior
`native-comparison-20261023.json` remains historical evidence for its original
receipt and source head; current native replay must pass separately in CI.
