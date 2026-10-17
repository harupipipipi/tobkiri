# DeepThink Wasm Pack

This ordinary `local.*` Pack follows Issue #1520: an open Flow role selects a
verified Wasm operation, which calls typed ports through Core's mandatory Gate.
It contains no Python runtime, native executor, network connection or secrets.
The Profile selects the actual model and tool providers and grants the limits.

The workflow decomposes the Goal, defines each abstract quality term separately,
confirms criterion IDs, checks prerequisites, plans sections, evaluates actual
capabilities and selects tools/skills. It checks the real work environment and
declarative Profile steps, researches references when required, gathers evidence,
produces sections one at a time, integrates them, then independently reviews and
revises. Approval requires every Goal criterion exactly once. Review failures keep
all candidates quarantined and produce no assistant answer.

Every model result is strict JSON with a concise decision summary. Private
reasoning is neither requested nor emitted. No phase imposes an output-token
limit. A typed zero token setting means the provider must omit its wire limit.

Each completed model generation, usage receipt and phase transition is persisted
in this Pack's private Core store. `goal.json`, `goal.md`, drafts, quarantined
candidates and final answers are projections of one atomic checkpoint value.
Repeating the identical input resumes an interrupted run. `workflow.status`
exposes the run reference, Goal, progress, actual usage and verified artifact
references. A changed model, input or captured config requires a distinct run.
For explicit continuation, the UI calls `continuation-input.submit` with the
run reference and conversation. `answers: null` resumes the original snapshot,
so later conversation history does not invalidate an interrupted run. A nonempty
answer is accepted only in `needs_input`: it reevaluates that paused phase and
its downstream dependents, retaining earlier Goal definitions, phase records,
usage and old cache generations. Changed model/config/permissions are rejected.
The continuation input always starts the Pack-owned continuation Flow, which
streams the workflow and appends a meaningful answer or resume marker to the
conversation. The UI does not call an execution method outside Flow.
Unresolved non-idempotent tool outcomes require reconciliation; they are never
automatically replayed. Missing provider cost receipts remain explicitly unknown.
The default review schedule is two reviews, configurable from one to five.

Tool calls are explicit JSON plans executed against the selected real tool
provider. They do not impersonate native model API tool calls. Actual artifacts
are verified by their owning tool's `artifact.stat`. The current harness tools
store logical text artifacts; they do not implement physical filesystem, shell,
PTY, 3D editing or Computer use. Those gaps are supplied as configuration and
must remain visible in capability assessment. No absent skill or Profile check
is reported as completed.

Builds/staging use `packs/build_deepthink.py`. Development scripts may use Python;
the runtime artifact is a Rust-built Wasm component with hash-bound WIT, config,
overlay and build provenance. Production activation still requires the Profile's
verification, conflict checks and approval; this README makes no migration parity
or successful Windows Core execution claim.
