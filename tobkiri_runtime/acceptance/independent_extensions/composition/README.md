# Independent extension evidence boundary

The same two authors used public docs/schemas/SDK and their own files only. They
did not read each other's implementation or Root's composition harness. Root
exposed a generic capability-free Python authoring API using the existing PackVM
ABI and real artifact compiler, then each author rebuilt independently. No
probe-specific branch was added to runtime/Host/Defaults.

The APIs are documented in `docs/python_pack_authoring.md`,
`docs/named_profile_authoring.md`, and `docs/public_conversation_lifecycle.md`.
Executable authoring
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
| Independent CLI component | 47 author tests including nine real arm64 executable subprocess fixture tests; exact Normal renderer and distinct source Application |
| Independent temporal component | 22 author tests; real compiler; threshold/DST/order/namespace and owner projection behavior |
| Individual, both, either removed, unselected | Real resolver, routes and OperationCatalog; exact selected bindings |
| Pack/Function ID rename | Generic re-authoring, signed admission and resolver; temporal author also tests renamed IDs |
| Unknown/duplicate/unapproved/conflicting selection | Exact resolver denial; valid signed conflicting Contract targets rejected by OperationCatalog |
| Missing signature / source and signature tampering | Actual admission denial including cryptographic signature mismatch |
| New named Profiles | Public producer, real compiler and unbound source artifacts; individual/both/unselected/renamed selection validated |
| CLI frontend and binary | Source CLI Profile replaces the Application/Shell selection; actual signed arm64 candidate passes public formal platform verification. Trusted publication/install/Host launch remains unperformed |
| Selected Workflow/prompt sources | Exact Pack pins and selected projection inventory verified; real Workflow compiler binds actual OperationCatalog principals; CapabilityCatalog reads selected system prompts |
| Issue1409 independent extension integration | Generic selected Workflow execution and saved-turn binding are implemented. Root integration uses the actual durable owner, Workflow engine and typed sink through explicit in-process test ports; this is not PackVM execution. Reducer inputs remain untrusted outside captured owner dispatch |
| Activation/Broker/PackVM/real provider API | Unperformed; native startup remains blocked |

`current-owner-named-profile-observation.json` records 75 expected offline outcomes,
including the earlier 28 signed Pack boundaries. It uses a read-only structural
Profile view derived from actual resolver output; no activation or live Kernel
authority is created. Workflow compilation performs no invocation or store write.
The preview palette includes the actual unchanged bundled owner binding required
by the new two-step source. Its structural Shell-to-step edges do not supply a
Workflow caller grant. The verified main Tauri bundle is a read-only template;
its older embedded runtime is not presented as executing the new Source bridge.
Both original independent input roots remain byte-exact. The older
`signed-named-profile-observation.json` and `signed-composition-observation.json`
remain historical evidence. Earlier
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
