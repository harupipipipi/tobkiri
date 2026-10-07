# Independent temporal extension — Issue #1409

A genuine executable Normal Sandbox Pack is produced entirely through the public
`core_runtime.pack_authoring` API documented in `docs/python_pack_authoring.md`.
**This independent extension's Issue #1409 integration is incomplete:** it is not
connected to the existing trusted lifecycle owner or native saved-turn context
bridge. Those public owner/storage/context paths already exist in the repository.
The component is offline and capability-free; authoring/compiler success grants
no admission, Profile selection, activation, approval, or execution authority.

## Exact declarations and resources

- Pack: `acceptance.temporal.context`, version `0.1.0`, `normal_sandbox`.
- Contract: `acceptance.temporal.context.v1`; operation `temporal.reduce`.
- Provider: `acceptance.temporal.context.provider`, sandbox / fail_closed.
- Function: `acceptance.temporal.context.reduce`, pure / pack_vm.
- ABI entrypoint: `tobkiri_packvm_invoke(operation_id, payload) -> dict` in the
  self-contained `source/temporal.py`, captured as `runtime/temporal.py`.
- Existing backend `tobkiri.python-pack-v4`, `runtime_abi: python3.13`, domain
  `sandbox.default.v1`, `materialization_mode: on_demand`, platform/architecture any.
- Empty effect/capability ceilings, no network, secrets, or Host bridge.
- `author_contract.py` renders exact draft schemas with the public canonical
  revision rule. The producer preserves `draft`; it does not invent acceptance.
- `build_pack.py` uses public `PythonPackFunction` / `build_python_pack`, which
  compiles fresh output before publishing it. Both Pack and Function IDs can vary.
- Temporal catalog resources are authored under
  `profile_projections/temporal/prompts/*.system.md` and `flows/*.flow.yaml`, and captured
  as immutable `content/*` assets. Prior loose drafts remain in `draft_resources/`.
  They have no automatic turn-lifecycle/prompt-consumer binding. Removing or
  replacing their selected consumer is Root's integration work, not demonstrated
  by simply including asset bytes.

## Useful reducer behavior

Input contains a namespace, ordered lifecycle event, and caller-owned snapshot.
These supplied values remain untrusted; this pure reducer cannot authenticate them.
An `assistant.completed` label replaces its illustrative completion baseline.
Tool completion, error, and cancellation consume event order without changing it.
A `user.received` event emits separate internal context only at 3600 seconds or more.
Offset-aware timestamps are compared in UTC, including DST. It never receives or
modifies user text, executes tools, performs network requests, or writes storage.
The output is the raw Contract outcome; it does not manufacture PackVM envelopes.

Composition must use the existing captured conversation owner for confirmed
completion and durable receipt timing, rather than creating a second timestamp
store. Caller-supplied labels and sequence values do not establish authenticity.
Snapshot serialization tests do not prove persistence or owner settlement.
Computing metadata in this reducer does not prove native model-context delivery.

## Build and verify offline

Run from `tobkiri_runtime` with the existing Python environment and bytecode disabled:

```sh
PYTHONDONTWRITEBYTECODE=1 /path/to/existing/python -B -m unittest discover -s acceptance/independent_extensions/temporal_context/tests -v
```

The public build API can be imported after adding this author directory to the
Python module search path; call `build_pack.build(new_output_path, pack_id,
function_id)` with a nonexistent output path. Ancestors must not be symlinks.
The checked-in `acceptance.temporal.context/` is fresh public-producer output,
not hand-sealed metadata or a fabricated release/activation lock.

Eighteen tests cover seven reducer scenarios plus deterministic byte-identical public
builds, actual public compiler routing, configurable ID compilation, actual named
Profile selected/unselected/removal/renamed source compilation, ABI outcome,
unknown operation and malformed input. Reducer scenarios include 3599/3600 seconds,
hours/day/week, offsets/DST, baseline reset, tools/errors/cancellation, no baseline,
corrupt snapshot, naive/invalid/backward timestamps, stale/duplicate events,
namespace isolation, snapshot round-trip, and input immutability.

## Remaining public integration gaps

The new authoring API resolves the earlier executable ABI/compiler authoring gap;
`compile_pack_root` now verifies the actual component. The prior official minimal
scaffold's empty executable catalog is no longer the final artifact.

`docs/public_conversation_lifecycle.md` and the dependency-free public SDK
`tobkiri_protocol.conversation_lifecycle` document existing owner-confirmed durable
completion, transaction-bound next-user receipt, UTC gap projections and separate
system-message context delivery in the saved-turn bridge. SDK functions project
owner data; they do not authenticate arbitrary mappings supplied by callers.

The trusted read source is the selected, captured `tobkiri.resource.conversation.v1`
Contract, Operation `rumi_conversation_store_pack.conversation-resource`, with
`operation: get` and the captured Profile/conversation identities. Its durable
owner validates final-turn evidence and rejects tool/wait/cancellation/stale
completion candidates. No ambient path or supplied completion flag replaces it.

The remaining extension task is to compose this independently selected Workflow
with that owner-bound source and the native saved-turn/request bridge, retaining
existing tool/approval policy and unchanged user text. This extension has not
verified those connections or native model delivery. The public named source SDK
also does not provide a complete CLI Application or live Shell binary. No broad
foundation changes or duplicate timestamp store are proposed.

Tests are authoring and offline compiler evidence, **not** signed admission,
Broker/VM execution, provider API acceptance, UI replacement, or live approval.
Root owns real runtime selection/admission testing separately.

## Actual named Profile source release

`named.profile.intent.v1.json` explicitly selects a new `acceptance.temporal.named`
Profile, published `defaults-basepack`, `shell.tauri.default` macOS/arm64,
`runtime.tauri.application.default`, this temporal Pack and its Pack-origin
`content` projection. `named-profile-release/` is genuine output from documented
`core_runtime.profile_authoring.build_named_profile`, using the exact locked
`ecosystem/defaultspack/v4` template and this Pack root. No generated release
artifact was edited by hand.

The compiler pins the selected source Pack artifact, subtree digest and three files;
its source lock retains `activation_authority: unbound`. Omitting the projection
produces no selected content; removing Pack and projection succeeds; removing the
Pack while retaining its projection fails closed. The actual error is preserved
in `removed-pack-projection-denial.log`. Rebuilding under new explicit Pack and
Function IDs and updating the intent/projection also compiles. None of these
source releases has a built Shell binary or runtime activation.

Prompt source is plain `prompts/acceptance.temporal.rules.system.md`; the filename
supplies the catalog ID, with no frontmatter. The legacy Flow YAML is retained as a
non-executable draft: actual v4 alias dispatch reports `V4_OPERATION_UNAVAILABLE`
according to Root's consumer verification. There is no public
`functions/*.function.yaml` registration format; none is invented.
Requested operation edges in the Tauri source intent remain empty, avoiding an
invented caller principal. YAML format and projection compilation do not prove
that a runtime Flow calls this reducer. Root verifies selected runtime resource
consumption separately. Connecting this extension to the existing trusted lifecycle
owner and native context bridge remains the Issue #1409 composition gap.

## Exact Workflow source intent

`author_workflow.py` creates public-schema-valid source at
`content/workflows/<pack-id>.workflow.intent.v1.json`, with API version
`io.tobkiri.profile-workflow-intent.v1`. The request pins the exact authored Function
ID, Contract ID/revision and `temporal.reduce`; it never contains a guessed runtime
principal. Source input uses public `${inputs.namespace}`, `${inputs.state}` and
`${inputs.event}` expressions. These supplied inputs remain untrusted: the file
does not establish completion provenance, durable state, or internal model input.

The selected compiler must match all four exact binding fields against its actual
captured operation palette, require exactly one candidate, derive that principal,
and run the real formal Workflow compiler. Root owns that active binding test.
This author uses public schema/producer/compiler tests without a fake engine,
principal, palette, approval, or Run. The legacy Flow remains a non-executable draft.

The 319-file generated release remains on disk and is ignored by this directory's
`.gitignore`. `named-profile-verification.json` contains concise real source pins.
Reproduce in fresh nonexistent outputs with `build_pack.build(new_pack_path)` and
`build_profile.build_profile(new_release_path, new_pack_path)` after importing this
owned author directory. Existing outputs are never overwritten by the producers.
