# Pack Architecture Wave 6 — Tool Runtime Ownership

Wave 6 replaces the defaultspack monolithic registry/executor path with a
manifest-selected, provider-neutral invocation pipeline:

```text
resolve definition -> validate arguments -> guard chain -> policy
-> authorize/approve -> select executor -> execute -> normalize -> audit/events
```

## Authoritative boundaries

- `rumi_tool_registry_pack`: revision-guarded definitions, schemas, aliases,
  widget metadata, migration, and rollback. It never executes a tool.
- `rumi_tool_broker_pack`: pipeline composition only. It contains no concrete
  tool, service, executor-pack, or domain-service branch.
- `rumi_tool_validation_pack`: deterministic, non-coercing argument validation.
- `rumi_tool_guard_pack`: ordered definition, caller, profile, permission,
  cancellation, and deadline guards.
- `rumi_tool_policy_pack`: policy over declared authority operations rather
  than tool IDs.
- `rumi_tool_approval_bridge_pack`: consumes core-authority one-shot tokens; it
  cannot issue or self-approve them.
- `rumi_tool_executor_selector_pack`: deterministic selection from activated
  provider metadata and execution-kind routing keys.
- `rumi_tool_result_pack`: provider-neutral results, errors, widgets, and
  executor provenance with secret-field redaction.
- `rumi_tool_audit_pack`: allowlisted lifecycle fields forwarded to the core
  audit owner. Raw arguments and results are forbidden.
- `rumi_tool_local_executor_pack`: invokes an exact selected local adapter.
- `rumi_tool_capability_executor_pack`: retains core trust and principal-grant
  enforcement.
- `rumi_tool_sandbox_executor_pack`: accepts only `python_docker` handlers and
  rejects host downgrade.
- `rumi_tool_remote_executor_pack`: invokes an exact selected remote adapter;
  it owns neither credentials nor network policy.
- `rumi_tool_mcp_executor_pack`: explicit MCP namespace and operation binding.
- `rumi_mcp_gateway_pack`: namespace-isolated MCP client call adapter. The old
  MCP connection runtime remains a finite compatibility source until Wave 10.
- `rumi_mcp_server_pack`: MCP server catalog/call projection back through the
  global broker; it owns no transport listener.
- `rumi_tool_authoring_pack`: definition-only validation and approved,
  revision-guarded publication. It accepts no source code or commands.
- `rumi_default_tool_projection_pack`: finite defaultspack migration and local
  execution adapter scheduled for Wave 10 removal.

Domain services remain independently available. Adapter removal removes only
their tool exposure. New tools publish a definition and an adapter provider;
the broker does not change.

Core/profile/frontend/chat/context/API-map consumers read definitions through
the finite global catalog client instead of importing the legacy ToolRegistry.
Direct legacy-registry imports remain only inside the old tool subsystem,
read/write compatibility blocks, and the catalog client's no-provider fallback;
those paths are scheduled for the Wave 10 facade cleanup.

## Global contracts

- `rumi.resource.tool.definition.v1`
- `rumi.action.tool.definition.manage.v1`
- `rumi.action.tool.definition.migrate.v1`
- `rumi.service.tool.invoke.v1`
- `rumi.service.tool.arguments.validate.v1`
- `rumi.service.tool.guard.evaluate.v1`
- `rumi.service.tool.policy.evaluate.v1`
- `rumi.service.tool.authorize.v1`
- `rumi.service.tool.executor.select.v1`
- `rumi.service.tool.execute.v1`
- `rumi.service.tool.local.operation.v1`
- `rumi.service.tool.remote.operation.v1`
- `rumi.service.tool.result.normalize.v1`
- `rumi.event.tool.invocation.v1`
- `rumi.service.mcp.tool.call.v1`
- `rumi.resource.mcp.server.tool.catalog.v1`
- `rumi.service.mcp.server.tool.call.v1`
- `rumi.service.tool.authoring.validate.v1`
- `rumi.action.tool.authoring.publish.v1`
- `rumi.resource.tool.migration.source.v1`

All executor implementations provide the same many-provider
`rumi.service.tool.execute.v1` contract. Manifest `routing_keys` declare their
execution kind. Exact matches precede wildcard providers; the selected provider
instance and content hash are attached to normalized results and audit events.

## Authority and approval

Policy recognizes separately:

- file read/write;
- shell inspect/execute;
- Git read/write/publish;
- browser observe/control;
- desktop observe/control;
- clipboard read/write;
- finite service, remote, and MCP projection scopes.

Unknown authorities and missing active-profile permissions are denied. Write,
execute, publish, control, clipboard, remote, MCP, and compatibility mutation
scopes require approval.

The approval bridge binds one-shot core-authority tokens to operation, canonical
argument hash, caller, profile, expiry, and replay policy. It returns an approval
request descriptor when no token is supplied and never trusts client `approved`
flags. Defaultspack removes caller/profile/token identities from untrusted
payload context before forwarding.

Every executor accepts calls only from `rumi_tool_broker_pack`. For the finite
legacy projection, the broker passes a secret-free authorization receipt through
the selected local executor. The projection converts it to the old internal
approval marker only after rechecking `authorized`, `consumed`, tool ID, caller,
profile, canonical argument hash, and one-shot replay policy. This avoids a
second legacy approval prompt without accepting a client-forged marker.

## Migration and rollback

The default tool projection produces a canonical, secret-free snapshot of
legacy IDs, input schemas, aliases, capability grants, risk, UI/widget metadata,
and adapter identity. The registry verifies the exact raw source hash before a
single owner-store write. It rejects missing alias targets and alias collisions,
stores an owner-only backup, and records a migration marker.

Rollback requires the exact marker, stores the migrated state as a rollback
copy, and removes only the new owner state. The compatibility projection and
legacy broker remain available for rollback until Wave 10.

Defaultspack invoke routes use the selected global broker. They fall back to the
legacy pipeline only when no global broker is selected. A global rejection,
missing migrated definition, policy denial, or missing executor never triggers
legacy fallback.

## Acceptance evidence map

| Requirement | Source evidence |
|---|---|
| Adding a tool requires no broker edit | definition contract, shared many-provider execute contract, manifest routing keys |
| No concrete broker branches | `rumi_tool_broker_pack/runtime/broker.py` and focused source test |
| Services independent from exposure | local/remote adapter contracts and removable projection packs |
| Fail-closed controls | ordered guards, active-profile permissions, policy, one-shot authorization, mandatory audit |
| Executor declaration | registry execution kind/contract/provider and selector |
| MCP isolation | explicit `mcp.<server>` namespace, exact gateway, executor-only consumer |
| Compatibility | source-hash migration of IDs/schemas/aliases/widgets plus result projection |
| Removal | adapter packs own no underlying domain state |

Focused tests are defined in `tests/test_pack_architecture_wave6.py` but were not
executed by the implementation agent.

テスト、lint、build、起動確認、実環境確認は実装担当のCodexでは実行していません。
マージ前に独立した実環境QAが必要です。
