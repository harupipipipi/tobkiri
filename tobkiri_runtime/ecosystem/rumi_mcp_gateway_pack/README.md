# Rumi MCP Gateway Pack

The canonical executable is a sandbox adapter for
`tobkiri.service.mcp.tool.call.v1` / `rumi_mcp_gateway_pack.mcp-tool-call`.
Its input is exactly `connection_id`, `tool`, and `arguments`. It requests only
`tobkiri.service.mcp.connection.v1` / `mcp.connection.call` through the authenticated
PackVM continuation channel. The Host owns approved connection settings, the
process, selected workspace, and originating Profile/session checks. Legacy
server registrations and caller-supplied approval fields do not authorize calls.
The Host MCP executor forwards the same finite input to the selected Gateway.

Ordinary Defaults activation/UI migration, full MCP process containment, and
native/external-provider acceptance remain incomplete. The declarative catalogs
below describe configuration assets; they are not connection authority.

`rumi_mcp_gateway_pack` is an optional ecosystem pack for broad MCP coverage when a server is not directly supported by a first-party Rumi pack yet. It is a catalog and gateway profile: it documents discovery, namespace routing, registry metadata, prompt/resource templates, and safety policy for unsupported MCP servers.

## What It Provides

- A local-first connector catalog for common MCP server categories.
- Namespace routing metadata for unknown servers and discovered tools.
- Marketplace and registry metadata for bundled discovery surfaces.
- Prompt and resource templates for reviewing, registering, and documenting MCP servers.
- Safety policies that keep unsupported MCP servers behind explicit namespace, approval, and audit boundaries.
- Conflict and overlap notes for `defaultspack` MCP interfaces such as `defaults.tool.mcp_connect`, `defaultspack.tool.mcp_connect`, `defaults.tool.mcp_list`, and `defaultspack.tool.mcp_list`.
- A finite, consumer-bound `call` adapter that validates namespace and operation
  syntax, then delegates to the existing `defaultspack` MCP client. The adapter
  accepts only the selected `rumi_tool_mcp_executor_pack` consumer.

## What It Does Not Provide

- No standalone MCP client, connector process, route, handler, credential store,
  discovery job, or network transport implementation.
- No replacement for `defaultspack` MCP execution, approval checks, registry persistence, or tool invocation.
- No secrets, credentials, API keys, OAuth material, remote endpoints, or server commands.
- No automatic approval for unknown, unsupported, or remote MCP servers.

## Docs

Start with [docs/README.md](docs/README.md), then read [docs/architecture.md](docs/architecture.md), [docs/interfaces.md](docs/interfaces.md), and [docs/operations.md](docs/operations.md).
