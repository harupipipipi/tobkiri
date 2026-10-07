# Independent temporal extension — Issue #1409

A genuine executable Normal Sandbox Pack is produced entirely through the public
`core_runtime.pack_authoring` API documented in `docs/python_pack_authoring.md`.
**Issue #1409 is not complete:** trusted completion events, durable snapshot storage,
hidden AI context insertion, and actual Defaults Flow integration remain missing.
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
- Temporal and removal Flow/prompt variants are authored under
  `profile_projections/temporal/` and captured as immutable `temporal/*` assets.
  They have no automatic turn-lifecycle/prompt-consumer binding. Removing or
  replacing their selected consumer is Root's integration work, not demonstrated
  by simply including asset bytes.

## Useful reducer behavior

Input is a trusted namespace, ordered authenticated lifecycle event, and caller-owned
snapshot. Successful final `assistant.completed` replaces the completion baseline.
Tool completion, error, and cancellation consume event order without changing it.
A `user.received` event emits separate internal context only at 3600 seconds or more.
Offset-aware timestamps are compared in UTC, including DST. It never receives or
modifies user text, executes tools, performs network requests, or writes storage.
The output is the raw Contract outcome; it does not manufacture PackVM envelopes.

The future composer must authenticate final-turn events, serialize them, and durably
commit snapshots within the profile/conversation namespace. Caller-supplied labels
and sequence values do not establish authenticity. Snapshot serialization tests do
not prove durable persistence. Computing internal metadata does not prove that a
model receives it as system/developer/runtime context.

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

Ten tests cover seven reducer scenarios plus deterministic byte-identical public
builds, actual public compiler routing, configurable ID compilation, ABI outcome,
unknown operation and malformed input. Reducer scenarios include 3599/3600 seconds,
hours/day/week, offsets/DST, baseline reset, tools/errors/cancellation, no baseline,
corrupt snapshot, naive/invalid/backward timestamps, stale/duplicate events,
namespace isolation, snapshot round-trip, and input immutability.

## Remaining public integration gaps

The new authoring API resolves the earlier executable ABI/compiler authoring gap;
`compile_pack_root` now verifies the actual component. The prior official minimal
scaffold's empty executable catalog is no longer the final artifact.

The public authoring docs explicitly do not provide conversation completion events,
durable storage, hidden-context insertion, CLI Shell/Application composition, or
automatic Flow/prompt binding. Those gaps still prevent Issue #1409 acceptance and
product Flow replacement. Required next public interfaces are authenticated final
lifecycle/user receipt delivery; namespace-bound atomic snapshot storage; separately
typed internal context model input; and a selected consumer for these immutable
Flow/prompt resources. No broad foundation change is attempted here.

Tests are authoring and offline compiler evidence, **not** signed admission,
Broker/VM execution, provider API acceptance, UI replacement, or live approval.
Root owns real runtime selection/admission testing separately.
