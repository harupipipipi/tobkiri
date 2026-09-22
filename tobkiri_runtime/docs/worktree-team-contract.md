# Evidence-bound worktree teams

`core_runtime.worktree_team_contract` is the provider-neutral boundary between a
PM's natural-language delegation and the runtime that creates a worktree or
starts a native/external worker. It records scope and evidence; it does not
create worktrees, launch models, push branches, or grant authority.

## Creation flow

1. The PM calls `normalize_task_request()` or `WorktreeTeamLedger.preview()` and
   shows the resulting manifest before task creation.
2. The manifest binds the task and PM IDs, role, model policy, exact starting
   commit/tree/ordered parents, clean state, file/field ownership, collision
   globs, dependencies, disk estimates, attempt budgets, forbidden capabilities
   and paths, evidence gates, and harness adapter.
3. `admit()` uses one SQLite transaction to verify predecessor PASS handoffs and
   reserve file, semantic-field, and collision-glob ownership. A conflicting
   request becomes `hold` and receives no claim or worktree authorization.
4. The worktree/agent owner may start only an `admitted` task and must preserve
   the manifest's `contract_digest` in its runtime record.

The same manifest is used for `native` and `external` harnesses. An external
harness supplies an `adapter_id`; neither form receives credentials, environment
maps, hidden prompts, or unrelated context through this contract.

## Attempts and blockers

`consume_operation()` binds commit, build, package, GUI, and push attempts to an
exact operation identity. Replaying the same identity returns its original
receipt; a new identity consumes another budget entry. An `indeterminate`
operation stays consumed until reviewed, so a vanished package/GUI/push lane
cannot be silently retried.

Gate rows are `PASS`, `FAIL`, or `UNVERIFIED`. The first `FAIL` requires one of:

- `product_source`
- `workflow`
- `harness_environment`
- `external_state`
- `policy`

Every later gate is automatically `UNVERIFIED`. This prevents a harness failure
from being relabeled as a product defect and prevents a partial run from being
summarized as an overall PASS.

## Completion and review

A completion packet always exposes exact input/output commit, tree, parents and
clean state; changed files and semantic fields; ordered commands and exit codes;
SHA-256 evidence references; the first blocker; consumed/remaining budgets; and
the complete gate matrix. Changes outside admitted ownership fail closed.

Successful work begins as `candidate`. Promotion proceeds without skipping:

```text
candidate -> reviewed -> stable -> final
```

Every promotion binds to the exact output digest. Rebase, changed parents, or a
semantic output change returns the task to `candidate` and marks its handoff
`UNVERIFIED`. Ownership is released only at an explicit clean boundary or when a
terminal task is archived.

## Presets

`worktree_task_preset()` returns mutable, vendor-neutral defaults for:

- `one_commit_implementation`
- `read_only_adversarial_review`
- `one_shot_package_gui`
- `integration_reconciliation`
- `external_state_recovery`
- `final_provenance_audit`

The PM fills identity, provenance, ownership, dependencies, estimates, forbidden
scope, and evidence requirements before normalization. Presets never hard-code a
model vendor or expand worker capabilities.

## Compaction and wakes

The SQLite ledger is authoritative across restarts and context compaction.
`wake()` returns only a concise status and `worktree-ledger:<task>:<revision>`
resume reference. The worker or PM reloads the full record from `get()` rather
than copying the original prompt into every wake message.

## Handoff example

```python
ledger.record_gate("task-api", "tests", "PASS", evidence_refs=["sha256:..."])
record = ledger.complete("task-api", {
    "overall": "PASS",
    "output": {
        "commit_sha": "b" * 40,
        "tree_sha": "c" * 40,
        "ordered_parents": ["a" * 40],
        "clean": True,
    },
    "changed_files": ["src/api.py"],
    "changed_fields": ["contract:api"],
    "commands": [{"argv": ["pytest", "-q"], "exit_code": 0}],
    "evidence": [{
        "kind": "tests",
        "sha256": "d" * 64,
        "location": "artifacts/test-report.json",
    }],
})
```

The evidence location is a bounded reference retained by the owning runtime; it
must not contain credential names/values or private environment data.
