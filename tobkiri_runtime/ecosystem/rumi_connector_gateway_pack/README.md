# Rumi Connector Gateway Pack

Rumi Connector Gateway Pack defines how Rumi should reason about external connectors and messaging channels without bundling credentials or transport code. It is inspired by OpenClaw and Hermes gateway patterns, but keeps Rumi's local-first grant model: installed connector tools execute; this pack owns namespace policy, scope cards, inbound-risk review, and handoff contracts.

## Required Secrets

None.

## Overlap Policy

- `defaultspack` owns approvals, grants, provider keys, active pack selection, scheduler routes, and MCP registry actions.
- Installed connector plugins own Slack, Gmail, Google Drive, GitHub, Notion, and similar transport execution.
- The `tobkiri.profile-content.local-agent.v1` Profile projection can consume normalized connector handoffs when reusable local-agent content is the best fit; it is Profile content, not an installable Pack or authority owner.
- This pack owns connector scope review cards, channel handoff envelopes, and inbound prompt-risk classification.
