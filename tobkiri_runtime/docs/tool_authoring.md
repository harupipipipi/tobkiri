# Tool Authoring

A tool needs a manifest, callable function or tool entrypoint, risk level, permission requirements, UI metadata, and model compatibility notes.

Function blocks are internal callable units. Tools expose user-visible capabilities and may be invoked by tool-calling models. High-risk tools include file writes, deletion, terminal execution, network mutation, browser/computer control, and credential changes.

Tool manifests should state required permissions, approval needs, input/output schemas, and UI labels. Tool-calling compatibility must be checked against selected model capabilities before the AI request is built.

## Contributing tool definitions (v4)

Packs publish definitions through the
`tobkiri.resource.tool.definition.contribution.v1` contract. Each definition
carries an `execution` descriptor that the Registry stores verbatim and the
executor/selector resolve against captured provider routes. A descriptor that
is catalog-visible but does not resolve to exactly one active provider route
fails closed: the tool appears in the catalog but can never be invoked.

### `kind: "local"`

```json
"execution": {
  "kind": "local",
  "contract_id": "tobkiri.service.tool.local.operation.v1",
  "provider_instance_id": "<pack_id>.<function-id>",
  "operation": "<pack_id>.<operation-id>"
}
```

- `contract_id` must be exactly `tobkiri.service.tool.local.operation.v1`.
- `provider_instance_id` and `operation` must be non-empty identifiers
  (`[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}`).
- `provider_instance_id` must equal the contributing Pack's Function ID (or its
  bound provider instance ID) and `operation` must equal the pack-qualified
  Operation ID declared in that Function's `executables.v4.json` entry.
  See `rumi_scheduler_tool_adapter_pack` for a working example:
  provider `rumi_scheduler_tool_adapter_pack.tool-adapter.scheduler`,
  operation `rumi_scheduler_tool_adapter_pack.scheduler-tool-operation`.

Enforcement: `rumi_tool_local_executor_pack/runtime/executor.py` and
`rumi_tool_registry_pack/runtime/selection.py` (`_available`).

### `kind: "mcp"`

```json
"execution": {
  "kind": "mcp",
  "contract_id": "tobkiri.service.mcp.tool.call.v1",
  "provider_instance_id": "<captured mcp.tool.call provider instance>",
  "namespace": "mcp.<server>",
  "operation": "<remote tool name>",
  "connection_id": "<connection identifier>"
}
```

- `namespace` must match `mcp.<server>` (`^mcp\.[a-z0-9][a-z0-9._-]{0,127}$`).
- `operation` names the remote tool; the selected route operation is fixed to
  the gateway operation `rumi_mcp_gateway_pack.mcp-tool-call`.
- `connection_id` binds the call to an admitted MCP connection.

Enforcement: `rumi_tool_mcp_executor_pack/runtime/executor.py` and the same
`_available` selector.

## Fixed runtime vocabularies

These vocabularies are sealed in source code. They are not configuration and
cannot be extended from a Pack manifest or definition. Unknown values fail
closed (selection reports the tool as unavailable; executors reject the
payload).

| Vocabulary | Location | Values |
|---|---|---|
| Selectable `execution.kind` | `ecosystem/rumi_tool_registry_pack/runtime/selection.py` | `local`, `mcp` |
| Policy authorities | `ecosystem/rumi_tool_policy_pack/runtime/policy.py` (`_AUTHORITIES`) | `file.read`, `file.write`, `shell.inspect`, `shell.execute`, `git.read`, `git.write`, `git.publish`, `browser.observe`, `browser.control`, `desktop.observe`, `desktop.control`, `clipboard.read`, `clipboard.write`, `service.invoke`, `service.mutate`, `remote.invoke`, `mcp.invoke` |
| Approval-required authorities | `ecosystem/rumi_tool_policy_pack/runtime/policy.py` (`_APPROVAL_REQUIRED`) | `file.write`, `shell.execute`, `git.write`, `git.publish`, `browser.control`, `desktop.control`, `clipboard.read`, `clipboard.write`, `service.mutate`, `remote.invoke`, `mcp.invoke` |
| Trusted tool Pack IDs | `ecosystem/defaultspack/domain/tool/security.py` (`TRUSTED_TOOL_PACK_IDS`) | `defaultspack`, `rumi_default_tools_pack` (plus the `core_runtime.pack_trust` check) |
| Authorable legacy `execution.type` | `ecosystem/defaultspack/domain/tool/security.py` (`SUPPORTED_AUTHORABLE_EXECUTION_TYPES`) | `rumi_function`, `capability`, `mcp`, `global_contract` |
| Trusted legacy `execution.type` | `ecosystem/defaultspack/domain/tool/security.py` (`TRUSTED_LEGACY_EXECUTION_TYPES`) | `local`, `handler`, `dynamic` — trusted first-party definitions only |
| Risk levels | `ecosystem/defaultspack/domain/tool/security.py` (`VALID_RISKS`) | `low`, `medium`, `high`, `critical` |
| Defaultspack tool→function map | `ecosystem/defaultspack/domain/function_runtime/registry.py` (`TOOL_FUNCTION_ACTIONS`) | fixed `tool_*`/browser/computer ID → Function action pairs |

Notes:

- `kind: "dynamic"` is retired. Dynamic Python tools are no longer registered
  (`register_dynamic` returns a `migration_required` error); new saved
  definitions must use a selectable kind with a resolvable route.
- `TOOL_FUNCTION_ACTIONS` is a separate legacy/defaultspack projection table.
  It maps fixed built-in tool IDs to defaultspack Functions; it is unrelated to
  the v4 `execution` descriptor and is not a contribution extension point.
- Other executor Packs exist (capability, sandbox, remote), but the v4 saved-
  tool selection path currently routes only `local` and `mcp` definitions.

## Extending a fixed vocabulary

Adding a new authority or a new selectable `execution.kind` is a coordinated
sealed-code change, not a Pack contribution. At minimum it touches:

1. `ecosystem/rumi_tool_policy_pack/runtime/policy.py` — `_AUTHORITIES`,
   `_APPROVAL_REQUIRED`, and `_risk` for a new authority.
2. `ecosystem/rumi_tool_registry_pack/runtime/selection.py` — the kind set,
   route contract, executor Pack/Operation convention, and `_available`.
3. The executor Pack that dispatches the new kind (provider contract,
   descriptor validation, consumer allowlist).
4. Contract catalogs, pack manifests, and generated artifacts:
   `migrate_pack_artifacts_v4.py`, `generate_executable_source_registry_v1.py`,
   `generate_executable_catalogs_v4.py`, `generate_defaultspack_v4_bundle.py`,
   and the pinned manifest/evidence files (`just generated-check`).
5. Focused tests for dispatch, selection availability, and fail-closed
   rejection of unknown values.

Pack authors should not invent vocabulary values. Contribute definitions
through the existing contract/provider/operation extension points: declare the
Function and Operation in `pack.v4.json`/`executables.v4.json`, then point each
definition's `execution` block at that exact declared route.
