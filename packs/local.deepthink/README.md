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
The optional `local.harness-chat.structured-llm-provider` port carries the actual
phase schema as typed JSON. It asks the provider for `json_schema` with `strict`
but always validates locally: a successful HTTP response is not proof of schema
compliance. `structured_provider=true` requires that binding and never silently
falls back to a less restrictive provider operation. Existing plain chat Prompts
and the ordinary `llm-provider` interface remain compatible.

Protocol `deepthink.workflow.v4` records typed capability gaps. Only a blocking
gap tied to an actual Goal requirement pauses for input; optional limitations
retain their real fallback plan. Old v2/v3 checkpoints remain retained and cannot
be silently upgraded or mixed with this schema/configuration.
Malformed or schema-invalid public responses have at most two automatic retries
by default (`max_contract_retries`: 0..3). Every rejection and actual usage receipt
remains durable, including across restart. Semantic errors, review rejection,
unresolved tool effects and any phase that already executed a tool are not
automatically retried. Explicit correction after a tool effect must return its
final contract without further tool requests; attempted effect replay is refused.

Each completed model generation, usage receipt and phase transition is persisted
in this Pack's private Core store. `goal.json`, `goal.md`, drafts, quarantined
candidates and final answers are projections of one atomic checkpoint value.
Repeating the identical input resumes a run only when its earlier model and tool
attempts have durably finalized outcomes. `workflow.status`
exposes the run reference, Goal, progress, actual usage and verified artifact
references. A changed model, input or captured config requires a distinct run.
For explicit continuation, the UI calls `continuation-input.submit` with the
run reference and conversation. `answers: null` resumes the original snapshot
when it has no unresolved attempt,
so later conversation history does not invalidate an interrupted run. A nonempty
answer is accepted only in `needs_input`: it reevaluates that paused phase and
its downstream dependents, retaining earlier Goal definitions, phase records,
usage and old cache generations. Changed model/config/permissions are rejected.
The continuation input always starts the Pack-owned continuation Flow, which
streams the workflow and appends a meaningful answer or resume marker to the
conversation. The UI does not call an execution method outside Flow.
The native CLI can submit the same input through the ordinary User port:

```powershell
tobkiri --data-root 'C:\Tobkiri\host-data' profile invoke --profile PROFILE_ID `
  --shape local.deepthink.continuation-input@1.0 --op submit `
  --args-file ./resume.json
```

`resume.json` contains `{"run-id":"RUN_ID","conversation":"CONVERSATION_ID","answers":null}`.
Use the original run's conversation ID. The command uses the existing User
permissions and Gate, streams output until its real source finishes, and does
not repeat an invocation after an output or cleanup failure. Preserve its run
reference and inspect the saved state before retrying an uncertain effect.
Unresolved non-idempotent tool outcomes require reconciliation; they are never
automatically replayed. Missing provider cost receipts remain explicitly unknown.
The default review schedule is two reviews, configurable from one to five.

Before a model request, v4 atomically saves an attempt bound to the exact run,
phase, request digest and captured model/provider configuration. While consuming
the response it saves versioned progress values in the same private Core store.
These contain only the provider generation ID, actual public model/provider
identity, public completion prefix, verified usage counters/cost and completion
evidence. Private reasoning, raw SSE and raw HTTP headers are excluded. Each
public batch is saved before the next blocking read. The OpenRouter
[`X-Generation-Id` header](https://openrouter.ai/docs/api_reference/streaming)
is delivered before reading the body and must agree with
SSE IDs; final usage repeating `finish_reason: stop` is counted as accounting,
not a second completion. A heartbeat without public content never means EOF.

`workflow.status` joins matching progress values and reports
`status: needs_reconciliation` with `model_attempts` when an attempt is unresolved.
Partials remain diagnostic evidence and cannot become an approved answer or a
validated phase cache entry. A pending legacy attempt, or a completed receipt
whose exact generation was not saved, also refuses continuation without sending
a new model request. Full public text and its usage become finalized in one
checkpoint. Unconfirmed checkpoint writes stop without an automatic retry or
replacement write. This version does not implement external provider generation
lookup or interrupted SSE reattachment: retain the run and inspect its receipt;
do not assume cancellation ended billing or repeat an unknown request.

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
verification, conflict checks and approval. Core startup and checkpoint persistence
alone do not establish successful model completion, tool execution or migration
parity. Native acceptance must record those results separately.
