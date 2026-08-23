# Interfaces

## Inputs

- Local user-supplied artifacts or records emitted by adjacent owner packs.
- Schema IDs listed in `ecosystem.json`.
- Evidence IDs, review state, and handoff owner labels.

## Outputs

- Draft packets.
- Review checklist packets.
- Handoff packets for owner packs.
- UI contract templates for host surfaces to render.

## Optional Integrations

- `rumi_default_tools_pack`: Owns concrete tool execution plus browser and desktop actions after the workroom emits approved handoff packets.
- `defaultspack`: Owns scheduler wakeups, durable run-adjacent persistence, and metrics receipts.
- `rumi_operations_team_pack`: Owns adjacent agent-service choreography and cross-worker PR/status orchestration after the workroom emits approved handoff packets.
- `rumi_model_catalog_pack`: Owns model availability and model-routing policy.

## Required Secrets

None.

## Does Not Provide

- tool execution
- browser action
- desktop action
- schedule execution
- file persistence
- metrics collection
- subagent PR management
- model routing
