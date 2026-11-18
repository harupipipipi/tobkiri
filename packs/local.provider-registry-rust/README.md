# Provider Registry translation candidate

The Registry and provider-configuration logic are translated from the existing
Python Pack. The production replacement still needs the Launcher settings and
credential-broker bindings.

`Registry<S, C>` also translates record normalization, six state operations,
Host request checks and redacted UI projection. The ordered `MemoryStore` is
the decoded-document test adapter. The optional `component` feature exports
the read-only UI projection and the internal Registry mutation port. It binds
requests to the authenticated Core execution Profile and stores the document
in that Profile's Pack-private SQLite store. New and concurrent updates use
atomic conditional writes. The model-access dependency uses its pure library.

`configuration::prepare_configuration` validates the request and freezes its
original canonical digest, normalized endpoint and Registry revision.
`execute_configuration` checks that plan again before requesting a credential.
An unknown create acknowledgement is neither retried nor compensated. After a
failed connection save, only a newly created handle proven absent from a
readable snapshot may be revoked. Credentialless local connections never call
the credential port. The separately granted Wasm `configuration.prepare-local`
port takes a typed proposal with no key or Profile selector. It reads the
active Profile's private Registry and returns the base plan without a write.
The existing GUI still applies its own generation budget and Broker lifecycle.
The separately granted `legacy-configuration.prepare-local` plans against the
existing Profile Registry. Its native caller supplies two independent read-only
observations from the verified legacy source. The Pack validates both against
the active Profile. A changed revision is rejected during preflight, and the
result contains the base plan only. This path is exercised with actual legacy
files in Core integration tests. Connecting the signed Launcher route and its
generation budget remains part of the product cutover.
The separate `configuration-commit.execute-local` port is exercised through
the trusted Core User entry. It binds the request to the canonical base plan
digest, stages the record, and returns success after the SQL compare-and-swap.
It does not retry or infer that an unconfirmed write succeeded. The existing
GUI's approved-operation status and credential transport still need to be
wired to the new runtime.
Local known policy/CAS failures short-circuit before a
snapshot read; remote failures retain the original compensation evidence order.

Run the focused native tests from the repository root:

```sh
cargo test --locked --manifest-path packs/Cargo.toml \
  -p tobkiri-pack-provider-registry-rust --features component --lib
```

Build the component and author the Registry Pack archive to verify local
preparation against the original Python
plans, plus the separately approved save, stale-plan refusal, Profile
isolation, and recovery through Core:

```sh
cargo build --locked --manifest-path packs/Cargo.toml \
  --target wasm32-wasip2 --release -p tobkiri-pack-provider-registry-rust \
  --features component
cargo build --locked -p tobkiri-cli --bin tobkiri
registry_work="$(mktemp -d)"
python3 -B packs/stage_registry_packs.py --tobkiri target/debug/tobkiri \
  --components packs/target/wasm32-wasip2/release --pack provider-registry \
  --output "$registry_work/registry-packs"
TOBKIRI_TEST_REGISTRY_PACKS="$registry_work/registry-packs" \
  cargo test --locked -p tobkiri-e2e --test provider_registry -- \
  provider_registry_uses_gated_private_storage_and_profile_revisions --ignored --exact
```

Building all three components and omitting `--pack` stages the Provider,
Model, and Saved Conversation archives used in the saved-model CI. The
receipt records the archive hashes and Core manifest pins. The `local.*`
identities use the Core local Pack policy.

The settings adapter still needs to supply the existing interactive approval,
operation identity, budgets, pending-effect lifecycle and credential transport.

The fixed `evidence/configuration-prepare-python.json` records 23 actual
preparation outcomes from unchanged original Python 3.13 source. Its test checks
the fixture hash, all 346 source hashes and producer hash, then compares native
results, errors and request digests with those observations. Since the live
Python tree continues to change, the tests can verify changed files from the
original capture commit `98bb10ab67480d69fee041a08d1516cafb544ff3`. Preparation
writes no Registry document. Execute tests currently exercise fault-injected
credential ports.

The ordinary package tests include this comparison. The broader decoded
Registry source consumer remains separately invoked with its authenticated
receipt and private evidence directory; an ignored test is not a passing replay.
