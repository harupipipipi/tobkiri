# Pure usage-cost Rust/Wasm candidate

This independent Rust candidate translates the complete pure projection in
`../local.ai-usage-cost-python/python/original_usage_cost.py` and its finite DATA
adapter. Its identity is `local.ai-usage-cost-rust`, Cargo package
`tobkiri-pack-ai-usage-cost-rust`; `runtime-info` explicitly reports Rust.
The compatible export remains
`local:ai-usage-cost-python/usage-cost@1.0.0`. The historical WIT namespace does
not claim that this component runs Python. Existing Python/WIT/oracles remain
unchanged. Core lifecycle is resource-free.

The five existing aliases are accepted: `calculate`, `normalize`, the manifest
base `rumi_ai_usage_pack.ai-usage-cost` (the original Host maps this to calculate),
and its `.generate` / `.stream` aliases. This is cost projection only; it has no
billing, reservation, Host, credentials, broker, or permission authority.

`ordered-python-compat` supplies the shared ordered-finite-json-v2 parser and
encoder, Python float conversion, truthiness and ordered nested string/repr
behavior. There is no second JSON codec in this provider. Primary key presence
beats fallback even for explicit null. Bool becomes float; absent/null/list/dict
and invalid or nonfinite numeric strings become unknown. Huge-int float overflow
is Arithmetic, not unknown. Negative zero remains a float with its sign. The
original two multiplies/addition and eight-field output order are preserved;
nonfinite arithmetic is an error, never manufactured zero/null/success.

Boundary errors retain the original fixed WIT mappings: Input → `invalid` /
`invalid usage cost request`; Validation, Arithmetic and Output → `internal` /
`usage cost provider {validation,arithmetic,output} failed`; Resource →
`limit-exceeded` / `usage cost resource limit exceeded`. No private exception
text is returned. A shared-codec fallible allocation failure maps to Resource.
Validation and Resource mappings do not claim that finite native DATA can reach
every Python exception class, or that every Rust allocation failure or Wasm trap
can be caught as a Python MemoryError. Core's original runtime limits/failure
custody remain required. Neither runtime resource acceptance nor every deep-input
interpreter limit has been established.

The manifest inherits the existing Packs workspace and pinned wit-bindgen 0.62.
Root owns workspace membership, dependency resolution and production packaging.
The macro reads the unchanged canonical Core WIT and existing provider WIT; it
does not stage or rewrite dependencies. The `rlib` output is for native source
differential testing; the `cdylib` output is the Wasm component candidate.

`tests/oracle_receipt.rs` consumes a separately generated
`io.tobkiri.python-provider-compat.v1` receipt from
`tools/quality/python_provider_compat/generate.py`. The original Python adapter
produces expected raw bytes and closed errors; Rust does not generate or update
them. The test verifies sidecar bytes/SHA-256 and current original source pins,
then compares every usage-cost result byte for byte and every error code/message.
It requires all five aliases and an arithmetic-failure vector. Receipt metadata
uses dev-only serde_json; product DATA still uses only the shared compat codec.
The reader also verifies the aggregate source-manifest hash and requires actual
CPython 3.13 / Unicode 15.1.0 with the unchanged 4300-digit limit. It does not
require a fabricated recursion depth to make deep-input differences disappear.
After Root's workspace integration and independent oracle generation:

```sh
TOBKIRI_USAGE_COST_ORACLE_RECEIPT=/absolute/path/to/receipt.json \
TOBKIRI_USAGE_COST_ACTUAL_DIR=/absolute/private/path/usage-rust-actuals-NEW \
  cargo test --manifest-path packs/Cargo.toml \
  -p tobkiri-pack-ai-usage-cost-rust --test oracle_receipt -- --test-threads=1
```

Both paths are required; absent evidence is a setup failure, not a skipped or
successful parity run. The actual directory must be new with an existing parent.
Every usage case runs, including native-recursion-edge inputs, before parity
failure is reported. Exact result UTF-8 bytes and fixed error JSON sidecars are
written with exclusive creation; compact `summary.json` records every comparison,
actual digest and the source/receipt binding. It never overwrites original
receipt, source, fixture or expected bytes. On Unix the new directory/files use
0700/0600 permissions. Logs contain only bounded case IDs, hashes or fixed errors.
Like any native test, a process abort or filesystem failure can stop the run;
partial artifacts do not count as a completed differential receipt.

This native source differential does not prove real
Wasm imports, guest execution, Core error projection, runtime resource limits,
size, Gateway composition or production activation. Those require Root's serial
component build, original corpus replay and existing Core acceptance paths.

No build, component execution, size result, composition, native Core acceptance
or activation is asserted here. Existing bridge 33-case evidence and finalizer
10-case five-Pack composition are distinct from new pure-provider differential
vectors. They must remain unchanged and be replayed separately. Full Gateway
migration, all original provider/Host behavior and existing owner/Gate/WIT custody
remain separate acceptance work.
