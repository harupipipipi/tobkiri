# Tobkiri Agent Control

An optional work-plan Pack for #1481–#1487. Goal, dependent Todo, scoped
instructions, reminders, settings and independent reviews share one persistent
plan per conversation. An absent/empty plan never registers a timer.

The Pack owns only its SQLite CAS/replay state. It reads conversations through
`tobkiri.resource.conversation.v1`, registers stable schedule records through
`tobkiri.action.schedule.v1`, resolves secondary models through
`tobkiri.resource.ai.model.profile.v1`, and executes work through
`tobkiri.action.turn.saved.v1`. It opens no other Pack's data files or imports its
implementation. There is no timer thread or network client in this Pack.

New conversation scopes default to the primary `conversation:<id>` executor,
an independent `review:<id>` reviewer context, and inherited model policy.
Existing explicit roles, settings and OFF are preserved. Tracked Goal/Todo
records reconcile one 600-second review reservation only through the selected
public scheduler and ordinary approved authority; this Pack grants no recurring
permission and enables no global setting.
Each review occurrence executes an editable captured Workflow v4 definition,
which invokes the Pack’s independent inspection operation with one attempt.
Ordinary edits keep its due time. A shorter interval can advance the next due;
a longer interval does not postpone an already due review. Explicit OFF and
pause survive later plan edits. Missing roles and failed schedule binding are
reported separately from persistence success.

The review uses a new, bounded, read-only AI context containing actual public
conversation/tool evidence, requested/resolved model policy and current plan
revisions. It records on_track, drift, blocked, unverifiable or review_failed.
Only supported drift creates executor guidance. Unresolved identical findings
coalesce by generation, Goal, condition and Todo identity as evidence changes;
receipt acknowledgement does not prove improvement. An independent on_track
recheck must name exact unresolved finding IDs, cite an actual new source
reference, and observe a strictly newer public conversation revision before
future projection stops for that guidance. The original finding, delivery and
resolution evidence remain in history. A recurrence creates one new delivery.
Reviewer context is capped at 128 KiB; larger uninspectable snapshots fail before
generation instead of silently dropping requirements or source evidence.

Immediate instructions, scheduled reminders and drift guidance use the same
durable inbox. Consumption is limited to before-turn/between-tool boundaries,
exact conversation/recipient/generation and input identity. Acknowledgements
are atomic and idempotent. Defaults appends instructions as a separately labeled
user context item without changing stored user text. Caller task context is
removed before the selected captured provider supplies its own projection. The
canonical coordinator acknowledges only a public conversation-owner saved
receipt whose immutable input digest matches the exact prepared context. Model
success alone never acknowledges a batch; failed calls retain it unconfirmed.
Paused/cancelled plans never wake merely because a reminder arrived.
Saved retries recover the exact owner-captured projection through the original
source input digest, including after restart or provider disablement. They do not
prepare a new inbox batch or repeat paid execution. A recovered projection without
a new verified acknowledgement remains visibly pending.

Snapshot model policy captures the model owner's version, resolved profile,
thinking level and store revision when configured. The receipt persists across
restart and is displayed through the public plan resource; callers cannot inject it.

Whole goal replacement uses prepare/commit. A preview binds plan, Goal,
conversation, proposed contents and expiry. The common commit operation
performs real gateway compaction and then atomically saves Goal/context plus a
checkpoint. Original transcript references, constraints and old evidence stay.
Compaction failure preserves the previous state. Restoring context cannot undo
external effects. Additional instructions never replace or recompact the Goal.

Todo execution immediately claims dependency-ready work and calls the existing
captured saved-turn tool loop for a primary chat bound as `conversation:<id>`.
Assigned-agent conversations remain explicitly unavailable on that current path. `tool_success:<exact registered tool operation>`
completion conditions are verified against actual successful tool receipts.
Other/natural-language conditions remain needs_review; assistant prose does not
mark them done. Waiting, cancelled, failed and ambiguous results are separate.
A retained ambiguous dispatch is not blindly repeated after restart.

## Integration and validation

`source_record.py` emits `pack-source.v1.json` for the integration owner to add
to the canonical Pack source catalog. Nine exact factories in `runtime/host.py`
require Broker admission and immutable Profile/root/domain capture. Resource
reads use read effects; context consumption, inbox ack and all mutations require
write effects. Whole-goal replacement and settings have distinct authority
operations; caller `approved`/actor fields are not admitted by the finite wire.
Frontend descriptors use catalog-bound generic sidebar/settings slots only.

Focused isolated tests:

```sh
PYTHONDONTWRITEBYTECODE=1 /opt/miniconda3/bin/python -B -m pytest \
  --noconftest tests/test_agent_control_pack.py tests/test_agent_control_acceptance.py -q
```

The saved-input and Workflow checkpoint passes 74 isolated tests, targeted Ruff and mypy. These
exercise real Pack state and deterministic fake external contracts/test tools;
they are not actual model, Pack activation or native-app acceptance evidence.
The normal packaged-fixture suite must run after integration regenerates the
source closure and catalogs. No signature/runtime receipt is fabricated here.

Remaining acceptance: canonical signed admission/activation, real independent
review→executor correction→evidence recheck, disable/re-enable/restart, model
availability and native sidebar/Composer interaction. The editable review Flow is
created through Workflow v4 with actual selected catalog digests and principals;
existing published user edits are preserved. Approval waits remain pending and
ambiguous attempts are never retried by a restart.
Legacy /goal model-loop and legacy Todo storage remain unchanged. Automatic
migration into a second Todo authority is deliberately unavailable; migration
needs an explicit public export/import handoff with one selected owner.
