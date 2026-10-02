# Tobkiri Agent Control

An optional work-plan Pack for #1481–#1487. Goal, dependent Todo, scoped
instructions, reminders, settings and independent reviews share one persistent
plan per conversation. An absent/empty plan never registers a timer.

The Pack owns only its SQLite CAS/replay state. It reads conversations through
`tobkiri.resource.conversation.v1`, registers stable schedule records through
`tobkiri.action.schedule.v1`, resolves secondary models through
`tobkiri.resource.ai.model.profile.v1`, and executes work through
`tobkiri.action.agent.run.v1`. It opens no other Pack's data files or imports its
implementation. There is no timer thread or network client in this Pack.

New tracked Goal/Todo records reconcile one 600-second review reservation.
Ordinary edits keep its due time. A shorter interval can advance the next due;
a longer interval does not postpone an already due review. Explicit OFF and
pause survive later plan edits. Missing roles and failed schedule binding are
reported separately from persistence success.

The review uses a new, bounded, read-only AI context containing actual public
conversation/tool evidence, requested/resolved model policy and current plan
revisions. It records on_track, drift, blocked, unverifiable or review_failed.
Only supported drift creates executor guidance. Unresolved identical findings
coalesce; receipt acknowledgement does not prove improvement.

Immediate instructions, scheduled reminders and drift guidance use the same
durable inbox. Consumption is limited to before-turn/between-tool boundaries,
exact conversation/recipient/generation and input identity. Acknowledgements
are atomic and idempotent. Defaults appends instructions as a separately labeled
user context item without changing stored user text. A successful provider
acceptance acknowledges that batch; failed calls leave it received/unconfirmed.
Paused/cancelled plans never wake merely because a reminder arrived.

Whole goal replacement uses prepare/commit. A preview binds plan, Goal,
conversation, proposed contents and expiry. The common commit operation
performs real gateway compaction and then atomically saves Goal/context plus a
checkpoint. Original transcript references, constraints and old evidence stay.
Compaction failure preserves the previous state. Restoring context cannot undo
external effects. Additional instructions never replace or recompact the Goal.

Todo execution immediately claims dependency-ready work and calls the existing
approval-aware agent/tool loop. `tool_success:<exact registered tool operation>`
completion conditions are verified against actual successful tool receipts.
Other/natural-language conditions remain needs_review; assistant prose does not
mark them done. Waiting, cancelled, failed and ambiguous results are separate.
A retained ambiguous dispatch is not blindly repeated after restart.

## Integration and validation

`source_record.py` emits `pack-source.v1.json` for the integration owner to add
to the canonical Pack source catalog. Eight exact factories in `runtime/host.py`
require Broker admission and immutable Profile/root/domain capture. Resource
reads use read effects; context consumption, inbox ack and all mutations require
write effects. Whole-goal replacement and settings have distinct authority
operations; caller `approved`/actor fields are not admitted by the finite wire.
Frontend descriptors use catalog-bound generic sidebar/settings slots only.

Focused isolated tests:

```sh
PYTHONDONTWRITEBYTECODE=1 /opt/miniconda3/bin/python -B -m pytest \
  --noconftest tests/test_agent_control_pack.py -q
```

The initial checkpoint passes 25 isolated tests, targeted Ruff and mypy. These
exercise real Pack state and deterministic fake external contracts/test tools;
they are not actual model, Pack activation or native-app acceptance evidence.
The normal packaged-fixture suite must run after integration regenerates the
source closure and catalogs. No signature/runtime receipt is fabricated here.

Remaining acceptance: canonical signed admission/activation, real independent
review→executor correction→evidence recheck, disable/re-enable/restart, model
availability, native sidebar/Composer interaction and editable Workflow binding.
Legacy /goal model-loop and legacy Todo storage remain unchanged. Automatic
migration into a second Todo authority is deliberately unavailable; migration
needs an explicit public export/import handoff with one selected owner.
