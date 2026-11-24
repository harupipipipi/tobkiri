> **旧仕様・移行用の参照資料**: この文書は Python runtime / Capability Graph / StartupProfile を前提にした旧アーキテクチャの記録です。現行の正の要件は [issue #1520](https://github.com/harupipipipi/tobkiri/issues/1520) です。新しい実装には [Rust 移行契約](../../docs/migration/1520/rust-contract.md) と [資源境界の修正](../../docs/migration/1520/resource-boundaries.md) を参照してください。この表示は移行の完了を意味しません。

# Capability Graph

Capability Graph is a capability wiring layer that sits beside the existing Execution Flow system.

Execution Flow remains responsible for ordered runtime procedures: startup, setup, handler execution, subflows, function calls, `python_file_call`, `universal_call`, scheduler integration, and explicit pipelines.

Capability Graph is responsible for declaring which runtime capabilities may be connected: AI clients, agents, tool bundles, memory, prompts, credentials, policies, frontend surfaces, CLI surfaces, and future pack-defined capabilities.

## Core Boundary

Core must stay domain-neutral. It may understand these generic concepts only:

- node
- port
- standard
- edge
- graph
- profile
- binding handler id
- validation result
- diagnostics

Core must not branch on domain meanings such as `agent`, `tool`, `ai_client`, `frontend`, `cli`, `memory`, or `prompt`. Domain-specific connection behavior belongs in ecosystem pack binding handlers.

Allowed core behavior:

- validate edge compatibility
- resolve an approved binding handler
- call that binding handler
- record diagnostics
- register graph/profile/runtime profile values in `InterfaceRegistry`

Forbidden core behavior:

```python
if target_node.kind == "agent" and source_node.kind == "tool":
    profile["agents"][target]["tools"].append(source)
```

## Files

Capability Graph files use `.graph.yaml`.

Initial discovery candidates:

1. `user_data/shared/graphs/*.graph.yaml`
2. `ecosystem/<pack_id>/graphs/*.graph.yaml`
3. `graphs/*.graph.yaml`

If duplicate `graph_id` values are discovered, Phase 1 treats that as a diagnostic error.

Pack-provided graph files are loaded only from packs that pass the existing pack approval and hash verification flow, following the same trust boundary as pack-provided Flow loading. User shared graph files are allowed as user-owned configuration, but they still require schema validation and diagnostics before registration or compile.

## Schema

Version: `rumi.graph.v1`

```yaml
graph_id: coding_workspace
version: rumi.graph.v1
display_name:
  en: Coding Workspace
  ja: コーディングワークスペース
nodes:
  - id: start
    ref: rumi.start
  - id: agent
    ref: defaultspack.agent
edges:
  - id: start_to_agent
    from: start.out
    to: agent.start
    kind: binding
```

`nodes[].id` is the graph-local instance id. `nodes[].ref` points to a node definition id. The same node definition may be instantiated multiple times in one graph.

Endpoint format:

```text
<graph_node_instance_id>.<port_id>
```

Phase 1 edge kind:

- `binding`

Reserved future edge kinds:

- `data`
- `event`
- `control`

Unknown edge kinds are errors in Phase 1.

## Validation

Graph validation checks:

- graph schema is valid
- all node refs exist in the global node registry
- all node refs are enabled by the selected profile when profile-aware validation is requested
- all edge endpoints parse correctly
- all referenced ports exist
- source port is `output`
- target port is `input`
- source and target standards intersect
- `multiple: false` input ports have at most one incoming edge
- `required: true` input ports have an incoming edge

Phase 1 required-port failures are validation errors. A future draft mode may downgrade them to warnings.

## Compile

Graph compile must be profile-aware from its first implementation.

Input:

```json
{
  "graph_id": "coding_workspace",
  "profile_id": "coding"
}
```

Compiler responsibilities:

- load graph and profile
- validate graph using the selected profile
- resolve node definitions
- call approved binding handlers
- produce a runtime profile dict
- derive `runtime_profile.launch.surface` when a frontend/surface binding
  points at a launchable surface node
- register `runtime_profile.<profile_id>.<graph_id>` in `InterfaceRegistry`
- return diagnostics

Compiler non-goals:

- no viewer UI
- no provider-specific tool schema conversion in the core compiler
- no domain-specific `agent/tool/ai_client` branching in core

## InterfaceRegistry Keys

Capability Graph related objects are registered using these key shapes:

```text
node.<node_id>
graph.<graph_id>
profile.<profile_id>
runtime_profile.<profile_id>.<graph_id>
```

## Core Node

`rumi.start` is the only special node owned by core. Core registers it before ecosystem node discovery.

`rumi.start` has one output port:

```json
{
  "id": "out",
  "direction": "output",
  "standards": ["rumi.flow.start"],
  "multiple": true,
  "required": false
}
```

All other nodes are discovered from approved ecosystem packs. Ecosystem packs must not override core-owned built-in node ids.

## Backend API

The backend exposes Capability Graph data through authenticated HTTP APIs. `/api/*` paths are the spec-facing API surface. `/api/panel/*` aliases return the same shapes for the control panel session and CSRF flow.

Read APIs:

- `GET /api/nodes`
- `GET /api/nodes/{node_id}`
- `GET /api/profiles`
- `GET /api/profiles/{profile_id}`
- `GET /api/profiles/{profile_id}/nodes`
- `GET /api/graphs`
- `GET /api/graphs/{graph_id}`

Graph preview APIs:

- `POST /api/graphs/{graph_id}/validate`
- `POST /api/graphs/{graph_id}/compile`

The viewer-facing node responses include locale-resolved labels, ports, standards, aliases, bindings, metadata, requirements, permissions, and profile node state when a profile is selected. The profile node API also returns `palette_nodes`, which contains only installed and profile-enabled nodes so the viewer does not need to hardcode node types.

The compile endpoint is a preview by default in the panel alias; callers can compile without replacing the launch-time startup profile source of truth.

Compile responses include `surface_launch_target` when the runtime profile
contains a launchable frontend surface. This is the same canonical payload used
by Startup Profile restart handoff:

```json
{
  "kind": "desktop_app",
  "pack_id": "frontendpack",
  "principal_id": "frontendpack",
  "surface": "browser",
  "node_instance_id": "frontendpack_web_surface",
  "node_id": "frontendpack.web_surface",
  "component_full_id": "frontendpack:frontend:web",
  "source": "capability_graph"
}
```

## Viewer Node Manager

The initial Node Manager is a profile-scoped catalog, not a graph editor replacement. It displays:

- Capability Graph profiles
- profile-enabled palette nodes
- installed, disabled, missing, unapproved, and missing-config states
- node ports, standards, aliases, bindings, and metadata
- graph validate and compile preview results

Profile clone controls are shown only when the selected Capability Graph profile has `permissions.can_create_profile: true`. This permission is still a preset/UI gate; privileged writes remain behind the existing authenticated panel API and filesystem controls.
