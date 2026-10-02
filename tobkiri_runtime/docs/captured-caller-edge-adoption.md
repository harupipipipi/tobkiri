# Exact caller operations in signed Profiles

A multi-operation Function can declare an outgoing edge with
`caller_contract_id` and `caller_operation_id`. Supply both or neither.
`caller_contract_revision_digest` is an optional additional exact pin and
requires the pair. The selector only narrows an operation already selected
by the Profile; it never selects another executable or grants its siblings.

Without the selector, the existing exactly-one-selected-principal rule remains.
The four-part `caller|target|contract|operation` authority-reference key retains
its original bytes. Explicit selectors extend the identity with the caller
Contract, operation and optional revision, and remain in the signed resolved
Profile and plan binding. They participate in the normal Profile definition,
edge-set, plan and confirmation digests.

The Host API is `core_runtime.profile_caller_edges_v4`:

```python
SelectedCallerOperationV4(contract_id, principal)
resolve_selected_caller(edge, selected_operations)  # existing FunctionPrincipal
```

Build candidates only from verified target operations in `plan.bindings` and
operations of the selected Shell artifact. A principal alone does not carry
a Contract ID; use the corresponding verified binding or Shell operation.
Do not include other provider operations merely present in an artifact.

The protocol helper `tobkiri_protocol.profile_edges` supplies
`captured_edge_identity(edge)` for caller/target-operation map keys,
`profile_edge_identity(edge)` / `profile_edge_key(edge)` for full edge identity,
and `require_profile_edge_bindings(edges, bindings)` for exact source-to-plan
selector, authority-reference, requested-scope and authority-mode lineage.
After resolution, also reject duplicate `(caller.principal_id, contract_id,
operation_id)` authority edges: a pinned and unpinned selector cannot duplicate
the same actual caller edge.

## Production integration owned by the root task

In `core_runtime/bootstrap/production_v4.py`:

1. Beside the Shell principal collection, retain selected caller candidates
   with `operation.contract_id`. Extend them from plan target bindings with
   the binding's exact `contract_id`; duplicate incoming edges are harmless.
2. Replace the caller Function count/first-element choice in the requested-edge
   loop with `resolve_selected_caller(edge, selected_callers)` and preserve its
   exact returned principal in ceilings, grants and captured edge records.
3. Use `captured_edge_identity` consistently in `binding_by_edge`,
   `seen_binding_edges`, `resolved_binding_by_edge`, `_CapturedPlanEdge.binding_key`,
   its lookup construction, backend materialization lookups and the provider
   metadata loop. Widen these tuple annotations to `tuple[str, ...]`.
4. Run `require_profile_edge_bindings` before capture and reject duplicate
   resolved authority keys even when their ceilings agree. Preserve exact
   caller-principal filtering in `select_edge`; never broaden it to Function ID.

No provider, Broker, pending-effect or Workflow implementation changes are
included here. Existing authority keys already contain caller principal IDs,
so advance and cancel remain distinct after explicit selection.

## Source and packaging adoption

Merge the source checkpoint, then regenerate the canonical source manifest
and Profile/catalog artifacts through the normal official generators. Old
Profiles remain unchanged; a Profile which adds the selector needs the usual
fresh resolution, exact confirmation and activation. Do not reuse a previous
authority reference or claim an unselected operation is admitted.

`scripts/build_packvm_guest_bundle.py` explicitly adds the pure
`tobkiri_protocol/profile_edges.py` and its `ids.py` dependency. The Host-only
`core_runtime/profile_caller_edges_v4.py` is not included in the guest closure.
This addition must be preserved beside the Stream worker's protocol additions.

Focused normal-fixture checks:

```sh
PYTHONDONTWRITEBYTECODE=1 python -m pytest \
  tests/test_profile_caller_edges_v4.py tests/test_profile_source_update.py \
  tests/test_tobkiri_host_v4_composition.py -q
```

Compiler tests use the ordinary packaged Profile fixture and real existing
file-inspect operation declarations, with isolated candidate Profile data.
No model execution or external effects occur.
