# Integration input

Canonical source record: `pack-source.v1.json` (regenerate with source_record.py).

Factories: `ecosystem.tobkiri_agent_control_pack.runtime.host:HOST_PROVIDER_FACTORY`.
All keys are `tobkiri_agent_control_pack.work-plan.<kind>`.

| kind | contract | operation | effect |
|---|---|---|---|
| resource | tobkiri.resource.work-plan.v1 | tobkiri_agent_control_pack.work-plan-resource | read |
| action | tobkiri.action.work-plan.v1 | tobkiri_agent_control_pack.work-plan-action | write |
| settings | tobkiri.action.work-plan.settings.v1 | tobkiri_agent_control_pack.work-plan-settings | write/approval aware |
| replace | tobkiri.action.context.replace.v1 | tobkiri_agent_control_pack.context-replace | write/approval aware |
| context | tobkiri.resource.context.projection.v1 | tobkiri_agent_control_pack.context-project | write (receive) |
| inbox | tobkiri.action.agent.inbox.v1 | tobkiri_agent_control_pack.inbox-action | write (ack) |
| execute | tobkiri.action.work-plan.execute.v1 | tobkiri_agent_control_pack.work-plan-execute | write/approval aware |
| review | tobkiri.action.work-plan.review.v1 | tobkiri_agent_control_pack.work-plan-review | write/approval aware |
| job | tobkiri.action.job.adapter.v2 | tobkiri_agent_control_pack.work-plan-job | write/approval aware |

The job adapter supports `describe` and advertises `agent-control.review` and
`agent-control.remind`. Dispatch deduplicates the exact occurrence derived from
the Broker's idempotency key. Its public result never claims instructions applied
because a schedule fired. Root owns global canonical/executable/semantic registry,
manifest authority, Profile edges, generated panel/bundles and final artifacts.

Shared source blocks (approved by root): finite neutral saved_task_context
protocol/schema; saved guest request validation; saved_bridge readiness/generation
message projection from immutable outer input; optional coordinator wrapper in
rumi_turn_runtime_pack/runtime/input_context.py. The turn owner’s saved Host
factory must optionally consume tobkiri.resource.context.projection.v1 and
tobkiri.action.agent.inbox.v1 (both version ^1.0.0, cardinality one, required false).
Add those optional entries to root-generated declarations/selection edges.
Defaults compatibility context helpers remain local; the earlier gateway AI-success
ack hook has been removed. Only canonical saved_receipt acceptance authorizes ack.
No App feature branch is needed. Regenerate the neutral saved input schema into
saved_complete and turn-saved source declarations, executable schemas and guest
protocol source closure. Ordinary user content is never rewritten.

UI: two frontend/contributions descriptors bind generic sidebar and settings slots.
Each action derives missing per-request IDs from authenticated Host request identity.
Resource get_for_conversation returns a nonpersistent draft at revision zero.

The modern job adapter is version **2.0.0**; all other Agent Control
contracts remain **1.0.0**. It coexists with legacy mode-based adapter v1.
