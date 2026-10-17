# Goal contract review and bounded tool diagnostics

This document specifies the Pack behavior. Acceptance must be reported
separately for exact component hashes and hosts; it is not an end-to-end test
result.

DeepThink's Goal contract needs a meaning review before work can begin. A
well-formed JSON contract can still contradict the original task: for example,
requiring an intermediate sum to equal a final product. Structural validation
and the model's `confirmed` field do not establish semantic correctness.

These features remain ordinary Wasm Pack operations under
[Issue 1520](https://github.com/harupipipipi/tobkiri/issues/1520). The input
adapter enters Pack-owned Flows; the worker owns the Run and SQL repository.
The adapter has no storage, network, secrets or worker-call privilege. Existing
published `@1.0` interfaces keep their members and types. New independent
interfaces require ordinary Profile bindings and Gate approval.

## Review, revise, then work

The closed `deepthink.goal-contract-semantics.v1` policy reviews a draft before
preflight. A rejection permits one revision followed by a second review.
Another rejection stops the workflow. These limits are separate from final
answer review and JSON/schema correction limits; continuing a Run cannot reset
them. Each phase keeps the existing one-phase checkpoint and request deadline.

The review compares the candidate with the original user request, accepted
clarifications and Goal requirements. It checks requirement coverage and
faithfulness, satisfiability, measurement and target alignment, and dependencies
between intermediate inputs and final outputs. Candidate digests, complete
criterion/requirement ID coverage and existing source pointers are validated.
An approval requires every required check to pass and no outstanding issues
or clarifications. Revision preserves criterion identities and requirement
links. No task-specific arithmetic or vocabulary is a production gate rule.

Evidence pointers address existing child fields, using the original Goal or
candidate itself as the root. Examples are `/requirements/0/description` and
`/success_criteria/0/target`, with the index selected by the corresponding ID.
Whole-item pointers such as `/requirements/0` are rejected. The prompt and
format-repair error identify this requirement explicitly. Resolving a pointer
establishes its location; it does not establish the truth of a model's check.

All three new phases are declarative: they produce public decision summaries
and structured results, with no tool execution. They do not request private
reasoning. A model review is evidence, not a general proof that arbitrary
natural-language requirements are correct.

The original Goal, phase inputs, model attempts, usage, resolutions and tool
receipts are retained. Candidate versions and reviews are recorded separately.
Only an accepted candidate becomes the effective contract for new phases and
final answer review. Resuming a failed phase does not erase review history or
restore another revision's allowance. Existing captured requests are never
rebuilt with a changed contract.

## Explicit adoption by a paused Run

An older Run paused immediately after `goal_contract` has not passed this
gate. Ordinary continuation refuses to begin preflight. Inspect its status,
then explicitly select it through
`local.deepthink.goal-review-input@1.0.review`:

```json
{"id":1,"shape":"local.deepthink.goal-review-input@1.0","op":"review","args":{"selection":{"run-id":"<run digest>","conversation":"<original conversation>","expected-revision":"<status revision>","expected-snapshot-digest":"<status snapshot digest>","candidate-digest":"<original contract digest>"}}}
```

The strings above are placeholders, not a runnable grant. Use the exact current
SQL Head and the SHA256 of the original contract's reviewed JSON digest
preimage. The operation supplies no replacement contract, approval flag,
invented user answer or accounting. A changed Head, conversation, captured
Settings or candidate rejects adoption before a model request. Unresolved
generation/tool/commit outcomes also block it. Already-adopted or later-work
Runs cannot use adoption to reset the review policy.

Adoption saves the original pending work queue and installs the review queue in
one confirmed repository transition. Only a successful review restores work.
Unknown saves stop; neither adoption nor a matching stored claim reconstructs
an in-memory generation dispatch permit. Status remains available for diagnosis.

Use the generic native `tobkiri profile session --profile <profile>` host to
send each ordinary operation and inspect its result separately. Approve each
actual prompt. After a settled phase, status and the next continuation remain
distinct requests. Close the host session with a new increasing request ID and
`{"id":3,"close":true}`; an application completion frame alone is not EOF.

## Read stored tool commitments

Model text requesting a tool does not prove that it ran. The independent
`local.deepthink.tool-diagnostics@1.0.page` operation reads compact commitments
from an existing, exactly selected managed SQL Run:

```json
{"id":2,"shape":"local.deepthink.tool-diagnostics@1.0","op":"page","args":{"selection":{"run-id":"<run digest>","conversation":"<original conversation>","expected-revision":"<status revision>","expected-snapshot-digest":"<status snapshot digest>"},"offset":0,"limit":16}}
```

The dedicated Flow returns one closed `tool-receipts` worker envelope. Its
normal adapter requires Completed and successful EOF, then returns the inner
page as the User operation's public value. Keep those two transport layers
distinct when validating captures. The query does not create a Run
or SQL schema, claim a generation, invoke a tool, or authorize replay. It still
passes normal Gate and audit paths. A query's successful transport does not
prove cleanup of an earlier failed invocation.

Pages contain at most 16 entries and a 16 KiB envelope. Each entry reports an
ordinal, dispatch ID, request digest, stored state and optional outcome digest;
private position keys, arguments and outcome bodies are omitted. Pending
receipts keep a null outcome and remain unknown. Malformed or inconsistent
stored states fail explicitly. Read every page at the same Head, check unique
dispatch IDs and contiguous ordinals, and follow `next_offset` through the
terminal page. A bounded page does not imply constant-cost loading of the
underlying Run.

For a known invocation, independently hash the exact
`{"id":dispatch_id,"name":tool_name,"arguments":arguments}` preimage and
the expected stored outcome. The Pack uses raw lowercase SHA256 of UTF-8
`serde_json::Value::to_string()` with sorted object keys; do not substitute a
different float/canonicalization format. For calculator integer success the
outcome is `{"ok":true,"result":{"value":391}}`. Distinct dispatch IDs
with identical arguments remain distinct receipts. Storage order alone does
not prove execution order or use of a prior result.

This is a report by the selected verified Pack over its stored snapshot, not a
Core-sealed attestation of arbitrary external effects. Actual model-selected
tool execution, selected component hashes, transport completion and independent
result checks remain separate acceptance evidence. Full Defaults, other hosts,
legacy tool parity and Terminal-Bench are not certified by these operations.
