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
| job | tobkiri.action.job.adapter.v1 | tobkiri_agent_control_pack.work-plan-job | write/approval aware |

The job adapter supports `describe` and advertises `agent-control.review` and
`agent-control.remind`. Dispatch deduplicates the exact occurrence derived from
the Broker's idempotency key. Its public result never claims instructions applied
because a schedule fired. Root owns global canonical/executable/semantic registry,
manifest authority, Profile edges, generated panel/bundles and final artifacts.

Shared source blocks (approved by root): optional generic task_context argument
in agent runtime; Defaults instruction_context helper; small run_request insertion
near chat_reference_prompt; request-local field in StreamEngine and acceptance ack
in the AI gateway compatibility wrapper. All preserve original user-message bytes.

UI: two frontend/contributions descriptors bind generic sidebar and settings slots.
Each action derives missing per-request IDs from authenticated Host request identity.
Resource get_for_conversation returns a nonpersistent draft at revision zero.
