# Interfaces

## Flows, Functions, Handlers, Routes, Events, Stores

This pack provides `rumi.service.mcp.tool.call.v1` for namespace-isolated tool
calls from the selected MCP executor. It declares no HTTP route or store.

It references these existing runtime interfaces:

- `defaults.tool.mcp_connect`
- `defaultspack.tool.mcp_connect`
- `defaults.tool.mcp_list`
- `defaultspack.tool.mcp_list`

## Catalogs

- `catalog/connector_catalog.yaml`: MCP server categories, examples, discovery cues, required review fields, and default risk.
- `catalog/namespace_routes.yaml`: route rules for known, unknown, and unsupported MCP servers.
- `catalog/marketplace_registry.yaml`: local marketplace metadata for discovery UI and registry import.

## Profile

- `rumi_mcp_gateway.gateway_operator`: local-first profile for reviewing and routing unsupported MCP servers.

## Prompts

- `prompts/mcp_gateway_router.system.md`: behavior for selecting defaultspack MCP operations, explaining approval needs, and avoiding implicit trust of remote server claims.
- `templates/prompts/unknown_server_triage.prompt.template.md`: prompt template for reviewing an unknown MCP server before connection.

## Resource Templates

- `templates/resources/mcp_server_card.resource.template.yaml`: template for documenting a server descriptor, namespace, capabilities, grants, review status, and approval posture.

## Required Secrets

None.

This pack must not embed secrets, access tokens, API keys, OAuth client material, bearer credentials, private keys, or ready-to-run remote connector configuration.

## Network

The gateway itself declares no fixed network destination. Actual MCP server
network authority belongs to the reviewed connection descriptor and existing
connection runtime; the global tool broker and MCP executor preserve approval
and namespace checks before this adapter is reached.

## Grants

Installing this pack should not grant MCP execution power by itself. Unknown MCP servers require explicit namespace assignment and explicit approval before connection or tool execution.

## Dependencies

- Required: `defaultspack >=2.0.0`, because executable MCP connection, listing, registry, approval, and tool execution are defaultspack-owned.
- Optional: `rumi_default_tools_pack >=1.0.0`, when a surface wants concrete tool catalog integration alongside MCP gateway discovery.

## Defaultspack Overlap

Until Wave 10, defaultspack remains the finite MCP connection-runtime
compatibility source. This pack owns the global MCP call adapter and declarative
catalog; tool invocation authority remains in the global broker/core authority.

- If a user requests MCP connection or listing, route to `defaults.tool.mcp_connect` or `defaults.tool.mcp_list`.
- If an MCP server is directly supported by another pack, prefer that pack's explicit namespace and documentation.
- If an MCP server is not directly supported, assign a gateway namespace such as `mcp_gateway.<server_slug>` and keep tool calls behind discovered names such as `mcp__<server_id>__<tool_name>`.
- Do not collapse unknown servers into `defaultspack.tool.*` aliases other than the existing MCP connection/listing interfaces.
- Namespaces are labels, not permission boundaries. Approval and runtime policy remain the enforcement boundary.
