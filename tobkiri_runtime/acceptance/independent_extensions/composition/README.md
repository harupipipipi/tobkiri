# Independent extension evidence boundary

The same two authors used public docs/schemas/SDK and their own files only. They
did not read each other's implementation or Root's composition harness. Root
exposed a generic capability-free Python authoring API using the existing PackVM
ABI and real artifact compiler, then each author rebuilt independently. No
probe-specific branch was added to runtime/Host/Defaults.

The API is documented in `docs/python_pack_authoring.md`. Executable authoring
required this general SDK publication; subsequent independent builds use the
public API without private imports or hand-created integrity seals.

`observe_signed_composition.py` invokes real Ed25519 signing, the formal Host-owned
install-policy writer, signed external admission/CAS, the actual Defaults data
resolver, exact executable routes and OperationCatalog. It uses disposable
isolated Host state and an ephemeral test publisher/key. No private key or
credential is retained. The verified build's finite bundle and Shell artifacts
are read-only inputs; original Pack hashes are checked before/after. Negative
cases require the expected exception type and reason, not just any exception.

Opaque authority references supplied to the offline resolver are synthetic test
inputs, **not** live Kernel records, human approval or execution authority. No
activation, broker, backend, VM, OS approval or provider API is invoked. Results
prove compiler/admission/selection boundaries, not a running product.

| Requested check | Actual evidence |
| --- | --- |
| Generic executable ABI/digest generation | Public producer and actual compiler; stale revision/schema/source/binding/path denial tests |
| Independent CLI component | 14 tests; real python3.13 / tobkiri.python-pack-v4 metadata; pure ABI outcomes |
| Independent temporal component | 10 tests; real compiler; threshold/DST/order/namespace behavior |
| Individual, both, either removed, unselected | Real resolver, routes and OperationCatalog; exact selected bindings |
| Pack/Function ID rename | Generic re-authoring, signed admission and resolver; temporal author also tests renamed IDs |
| Unknown/duplicate/unapproved/conflicting selection | Exact resolver denial; valid signed conflicting Contract targets rejected by OperationCatalog |
| Missing signature / source and signature tampering | Actual admission denial including cryptographic signature mismatch |
| Named CLI Profile / actual frontend replacement | Unresolved intent; CLI Application/Shell composition incomplete |
| Flow/prompt replacement | Immutable authored resources, not bound runtime replacements |
| Issue1409 trusted completion/persistence/hidden context | Incomplete; reducer snapshot is caller-owned, not durable production state |
| Activation/Broker/PackVM/real provider API | Unperformed; native startup remains blocked |

`signed-composition-observation.json` records the final offline matrix. Earlier
`unsigned-admission-observation.json` is retained as historical evidence: its
absent-policy denial preceded signature/executable validation and is not positive
admission evidence.

Run from `tobkiri_runtime` using an existing interpreter and a verified build:

```bash
python -B -m acceptance.independent_extensions.composition.observe_signed_composition \
  --pack acceptance/independent_extensions/cli_presentation/acceptance.cli.presentation \
  --pack acceptance/independent_extensions/temporal_context/acceptance.temporal.context \
  --bundle-root /path/to/verified-build/bundled/dev-defaults/v4 \
  --artifact-root /path/to/verified-build/bundled/dev-defaults/platform-artifacts \
  --output /path/to/new-observation.json
```

No legacy fallback, in-process conformance backend, fake activation, weakened
approval, publisher policy or VM capacity guard is used. Product integration
remains separate from the offline successes.
