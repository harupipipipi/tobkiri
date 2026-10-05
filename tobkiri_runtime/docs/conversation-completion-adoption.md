The saved append clock is a candidate until the durable turn owner confirms a
completed terminal. This closes cancellation after final append but before
durable settlement. A durable execution can complete while its assistant is
waiting for a user or approval; those message states never create a candidate.

Review and promote the scoped additions in
`conversation-completion-adoption.v1.json` into the source-authoritative catalog
and semantic source fixture. No generated global artifacts are changed here.
The new Host function is loaded from the canonical absolute module
`ecosystem.rumi_conversation_store_pack.runtime.completion_host`. Its input is
only Profile, `operation: confirm` and turn ID. Caller-supplied terminal records,
completion flags and approval flags are rejected. Only captured saved/reconcile
turn functions may invoke it, and its nested client admits only the public turn
resource, with credentials disabled. Public source closure is finite:
saved/reconcile -> completion confirmation -> turn resource; the resource has
no dispatch edge.

The conversation owner checks Profile, exact saved request ID/input digest,
initial revision, immutable result reference, active user generation, assistant
head, actual assistant state and outcome digest under its own lock. Successful
promotion increments the conversation revision without rewriting the transcript
or append clock. Existing historical completed sources remain readable.

Confirmation is idempotent after a lost reply. A candidate that has been reset,
edited, replaced, deleted, branched or resumed cannot be promoted by an old turn.
Missing confirmation dependencies leave the durable turn completed and the
conversation candidate ineligible; the caller reports reconciliation required.
Reconciliation retries confirmation only, and cannot rerun the paid effect.

Validation is isolated source feedback with `pytest --noconftest`. Canonical
packaged/source closure tests require integrator regeneration. Computer Use and
real model verification have not been performed in this worktree.
