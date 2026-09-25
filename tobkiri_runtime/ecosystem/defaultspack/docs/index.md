# defaultspack Docs Index

Start here when navigating defaultspack docs. The canonical implementation is
`tobkiri_runtime/ecosystem/defaultspack/`.

Terminology in this section is intentional:

- `rule`: always-on instruction layer within a scope
- `skill`: trigger-based or on-demand instruction and workflow bundle
- `prompt`: a source asset or rendered model text assembled at runtime
- `system prompt`: the low-level API/runtime term for system-role prompt text
- `delegation`: the canonical action for sending work to another agent
- `team workspace`: the user-facing name for the company coordination surface;
  internal ids and routes may still say `company` for compatibility

For the repo-wide glossary and migration guidance, see
[`../../../docs/terminology.md`](../../../docs/terminology.md).

## Orientation

| Topic | Document |
|---|---|
| PR97 architecture overview | [defaultspack-explained.md](defaultspack-explained.md) |
| Getting started | [getting-started.md](getting-started.md) |
| Runtime architecture | [architecture.md](architecture.md) |
| Local-first policy | [local_first_policy.md](local_first_policy.md) |
| Safety and permission audit | [safety_permission_audit_design.md](safety_permission_audit_design.md) |

## User-Facing Systems

| Topic | Document |
|---|---|
| Frontend shell and routes | [frontend.md](frontend.md) |
| Frontend extension points | [frontend_extensions.md](frontend_extensions.md) |
| RumiTemplate composition platform | [templates.md](templates.md) |
| UI and layout | [ui_and_layout.md](ui_and_layout.md) |
| History organization persistence and recovery | [history_organization_persistence.md](history_organization_persistence.md) |
| Chat module | [chat.md](chat.md) |
| Agent runtime | [agent_runtime.md](agent_runtime.md) |
| Team workspace runtime | [multi-agent.md](multi-agent.md) |
| Scheduler | [scheduler.md](scheduler.md) |

## Runtime Primitives

| Topic | Document |
|---|---|
| Tools | [tool.md](tool.md) |
| MCP | [mcp.md](mcp.md) |
| Flow engine | [flow.md](flow.md) |
| Prompt and system-prompt plumbing | [prompt.md](prompt.md) |
| Memory | [memory.md](memory.md) |
| Media | [media.md](media.md) |
| AI providers | [ai-providers.md](ai-providers.md) |
| AI client | [ai_client.md](ai_client.md) |

## Integration And Extension

| Topic | Document |
|---|---|
| Extending defaultspack | [extending.md](extending.md) |
| Template schema, trust, collision, lifecycle, and contracts | [templates.md](templates.md) |
| Input profiles | [input-profiles.md](input-profiles.md) |
| External inputs | [external-inputs.md](external-inputs.md) |
| Webhooks | [webhooks.md](webhooks.md) |
| Gateway | [gateway.md](gateway.md) |
| Transport | [transport.md](transport.md) |
| Capability dependency resolution | [capability/dependency-resolution.md](capability/dependency-resolution.md) |
