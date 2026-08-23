# Flow Graph compiler contract

The historical Launcher graph editor and `/api/panel/graphs` endpoints are
retired. They must not be restored as a second registry or execution path.
`tobkiri_workflow_pack` now owns the finite, authority-free
`graph.compile-preview` operation for clients that author `rumi_graph` version
1 documents.

## Input boundary

The schema is
`ecosystem/tobkiri_workflow_pack/schemas/rumi-graph.v1.schema.json`.

- The graph has exactly one `rumi_start` trigger and at least one reachable
  `step` node.
- Each step carries the complete Contract identity: contract ID, revision
  digest, operation ID, and function principal ID. There is no handler-name,
  Pack-name, legacy registry, or implicit fallback lookup.
- The compiler resolves that identity only in the activation-scoped Contract
  catalog captured for the current ProfileLock/ResolvedPlan. Compilation is
  pinned to its catalog digest and SecurityEpoch.
- A step without editor ports receives `contract-input` and
  `contract-output` ports from the captured operation's manifest-derived input
  and output schema digests. If a client supplies step ports, they must exactly
  match those captured contracts; trigger/end ports remain explicit. Missing
  handles, empty port contracts, and incompatible edges fail closed.

The compiler never reads an arbitrary Pack manifest at request time. This
prevents a changed or unselected Pack from becoming ambient authority.

## Graph-to-runtime mapping

`graph.compile-preview` emits a normal `io.tobkiri.workflow.v4` Definition and
the same deterministic `io.tobkiri.workflow-compile.v4` preview used by
`definition.compile-preview`.

- `rumi_start` and `end` nodes are editor structure and do not become runtime
  steps.
- Every step-to-step incoming edge becomes a sorted, deduplicated
  `depends_on` entry.
- A fork produces multiple dependency-ready steps. A join depends on every
  incoming step.
- A branch edge can become the target step's `when` only when it uses the
  Workflow v4 restricted expression subset and all incoming conditions map to
  one unambiguous expression.
- Cycles, unreachable steps, unknown endpoints, and branch shapes that cannot
  be represented losslessly are rejected.

The normalized semantic graph is returned for deterministic compiler round
trips and evidence snapshots. Visual layout/labels remain client-owned editor
metadata and are not executable artifacts. Port contracts establish editor
connection compatibility and ordering; they do not pipe a Provider's output
into another Provider's input.

## Simulation-only versus runtime-backed

| Surface | Semantics |
|---|---|
| Node positions, labels, and port display | Editor-only metadata; no effect and no authority reservation |
| `graph.compile-preview` | Runtime-backed validation and deterministic compilation against the captured active catalog; no Provider execution |
| `definition.create` / `definition.publish` | Workflow Pack persistence and publication; publish recompiles against the current captured catalog |
| `run.*` | Runtime-backed Workflow v4 state machine; every effect still uses the Authority reserve/commit and exact Contract Broker path |

There is no supported viewer execution simulation. A client may animate a
preview for presentation, but it must label that behavior as simulation and
must not claim a Provider ran. Later effects still use the exact Contract
Broker and each Provider's ResolvedPlan execution boundary (including PackVM
where declared). The graph compiler supplies no host execution fallback,
legacy lookup, implicit fallback, or second authority source.

## Conformance evidence

`tests/fixtures/workflow_v4_graphs/` contains a representative fork/join graph
and its reviewed runtime-step snapshot. `tests/test_workflow_v4.py` covers the
snapshot, deterministic normalized round trip, branching, `depends_on`,
manifest-derived ports, missing handles, mismatched contracts, cycles, and
exact-operation pinning.
