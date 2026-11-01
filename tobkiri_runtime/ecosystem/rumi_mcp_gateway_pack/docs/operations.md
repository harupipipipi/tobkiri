# Operations

## Installation

Install through the setup-pack selector as `rumi_mcp_gateway_pack`. The setup
metadata marks the pack as optional, critical risk, and not eligible for
automatic all-ok grants because it exposes an MCP invocation adapter.

Expected prerequisite:

- `defaultspack >=2.0.0`

## Development

Keep the executable surface limited to the verified, consumer-bound namespace
adapter in `runtime/gateway.py`. New MCP connector processes, routes, handlers,
stores, network transports, or background discovery jobs are outside this
pack's responsibility and require a separate implementation task.

When changing behavior, update the matching files:

- `catalog/connector_catalog.yaml` for category or discovery metadata.
- `catalog/namespace_routes.yaml` for route and namespace policy.
- `catalog/marketplace_registry.yaml` for marketplace or registry metadata.
- `policies/unsupported_server_safety.yaml` for approval, audit, or fail-closed behavior.
- `prompts/` and `templates/` for operator instructions and server documentation templates.
- `docs/interfaces.md` whenever dependencies, grants, network, or secrets posture changes.

## Tests

Run the focused contract test:

```bash
python -m pytest tobkiri_runtime/tests/test_rumi_mcp_gateway_pack_contract.py
```

Useful nearby checks:

```bash
python -m pytest tobkiri_runtime/tests/test_setup_pack_selector.py tobkiri_runtime/tests/test_defaultspack_mcp_registry.py
```

## Common Failure Modes

- A catalog entry includes a ready-to-run command, URL, or credential. Keep entries descriptive and non-executable.
- An unknown server is routed without a unique namespace. Use `mcp_gateway.<server_slug>` until a direct support pack owns it.
- A prompt implies that namespace alone is permission. Namespaces are compatibility labels; approval and grants enforce permissions.
- Setup metadata marks the pack all-ok eligible. Do not do this while unknown MCP servers are in scope.
- Documentation drifts from defaultspack ownership. `defaultspack` remains the owner of MCP connection, listing, registry, and execution.

## Change Review Checklist

- Required docs from `tobkiri_runtime/docs/pack-documentation-contract.md` still exist.
- The artifact manifest binds the finite gateway adapter and no standalone
  network connector code was added.
- No secrets or credential-like literals were added.
- Unknown MCP servers remain approval gated.
- `defaults.tool.mcp_*` overlap behavior is documented.
