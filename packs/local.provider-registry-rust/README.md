# Provider Registry translation candidate

The Registry and provider-configuration logic are translated from the existing
Python Pack. The application still uses its original setup and settings flow;
this crate is not yet registered as the production replacement.

`Registry<S, C>` also translates record normalization, six state operations,
Host request checks and redacted UI projection. Its ordered `MemoryStore`
distinguishes an absent document from an initialized store, but remains a
sequential decoded-document adapter. It implements no filesystem lock, atomic
replacement or Core transaction; native storage/authority failures still need
their own owners. A helper success is not evidence that a real write committed.
The model-access compiler's component feature is disabled only for this
dependency; existing component consumers keep their default build. This crate
has no registered Core contribution or Wasm export.

`configuration::prepare_configuration` validates the request and freezes its
original canonical digest, normalized endpoint and Registry revision.
`execute_configuration` checks that plan again before requesting a credential.
An unknown create acknowledgement is neither retried nor compensated. After a
failed connection save, only a newly created handle proven absent from a
readable snapshot may be revoked. Credentialless local connections never call
the credential port. Local known policy/CAS failures short-circuit before a
snapshot read; remote failures retain the original compensation evidence order.

Run the focused native tests from the repository root:

```sh
cargo test --locked --manifest-path packs/Cargo.toml \
  -p tobkiri-pack-provider-registry-rust --lib
```

The caller still owns approval, budgets, operation identity, pending-effect
retry/status, storage locking and real credential/network adapters. This crate
does not replace those owners or grant authority from a submitted plan.

The fixed `evidence/configuration-prepare-python.json` records 23 actual
preparation outcomes from unchanged original Python 3.13 source. Its test checks
the fixture hash, all 345 source hashes and producer hash, then compares native
results, errors and request digests with those observations. Preparation writes
no Registry document. Execute tests use explicit fault-injected ports and do
not establish real credential, persistent-store, Wasm or GUI integration parity.

The ordinary package tests include this comparison. The broader decoded
Registry source consumer remains separately invoked with its authenticated
receipt and private evidence directory; an ignored test is not a passing replay.
