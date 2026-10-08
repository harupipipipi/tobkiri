# Capability Profile Spec

Capability Profiles are runtime or workspace presets for Capability Graph compile. They describe which nodes are available and how a selected graph should run in a particular environment.

Version: `rumi.profile.v1`

Profiles are not security source-of-truth documents. Parsed profile permissions may guide UI and runtime defaults, but privileged operations must still be enforced by existing trust, grant, approval, and capability systems.

## Files

Initial discovery candidates:

1. `user_data/shared/profiles/*.profile.yaml`
2. `ecosystem/<pack_id>/profiles/*.profile.yaml`

Pack-provided profile files are loaded only from packs that pass the existing pack approval and hash verification flow, matching the trust boundary used for pack-provided Flow loading. User shared profile files are user-owned configuration, but they still require schema validation and diagnostics before registration or use.

## Relationship To Startup Profiles

Capability Graph profiles do not replace the existing `StartupProfileManager` or launch-time startup profile system in the initial PRs.

Until an explicit bridge or migration PR lands, existing startup profiles remain the launch-time source of truth for selecting startup behavior, setup, and runtime launch defaults. `rumi.profile.v1` is a graph/runtime preset used by Capability Graph loading, validation, compile, and viewer/node-manager filtering.

The profile loader adapts to the existing system by coexisting with it. It may read startup-related defaults for display or diagnostics only when explicitly wired, but it must not supersede startup profile selection.

The backend API exposes this relationship side by side:

```json
{
  "launch_time_source_of_truth": "StartupProfileManager",
  "capability_graph_profiles_role": "graph_runtime_presets",
  "startup_profile_api": "/api/panel/startup/profiles"
}
```

This is an explicit bridge contract for the viewer: startup profiles continue to own launch-time startup behavior, while `rumi.profile.v1` controls Capability Graph loading, palette filtering, validation, and compile preview. Replacing `StartupProfileManager` still requires a dedicated migration decision and PR.

Terminology:

- `StartupProfileManager` owns launch-time startup profiles such as `rumi_cli`, `rumi_desktopapp`, and `rumi_work`.
- `CapabilityProfileDefinition` owns `rumi.profile.v1` graph/runtime presets such as `defaultspack.coding`.
- `default_graph` on a Capability Profile is compile input only. Startup profile launch does not automatically compile that graph in this PR.
- Bridging startup profile launch to Capability Graph compile/runtime registration is intentionally out of scope until the launch contract is explicitly designed.

## Relationship To Graphs

Graph and profile are separate:

- Graph is the capability wiring diagram.
- Profile is the runtime preset, environment, permissions, defaults, and node availability for that wiring diagram.

The graph compiler always receives both `graph_id` and `profile_id`.

## Schema

```yaml
profile_id: coding
version: rumi.profile.v1
kind: runtime_profile
display_name:
  en: Coding
  ja: コーディング
locale: en
default_graph: coding_workspace
default_flow: coding_startup
enabled_nodes:
  - rumi.start
  - defaultspack.agent
  - defaultspack.tool.registry
disabled_nodes:
  - defaultspack.experimental.remote_shell
viewer:
  palette:
    include:
      - defaultspack.agent
      - defaultspack.tool.registry
permissions:
  can_install_packs: false
  can_create_profile: true
  can_update_profile: true
  can_delete_profile: false
policy:
  max_tool_calls: 8
  require_approval_for_tools: true
node_settings:
  defaultspack.agent:
    model_profile: default
```

## Required Fields

- `profile_id`
- `version`
- `kind`

## Common Fields

- `enabled_nodes`
- `disabled_nodes`
- `default_graph`
- `default_flow`
- `viewer.palette`
- `permissions`
- `policy`
- `node_settings`
- `locale`

## Node Availability

The profile-aware node registry is derived from:

```text
global node registry + selected profile
```

Phase 1 behavior:

- nodes listed in `disabled_nodes` are unavailable
- if `enabled_nodes` is non-empty, only listed nodes are available
- if `enabled_nodes` is empty or absent, all global nodes are available except disabled nodes

Graph validation and compile must reject graphs that use unavailable nodes.

## Node State

Profile node state should be computed separately from node definitions.

Expected state categories:

- enabled
- disabled
- missing_definition
- missing_configuration
- unavailable

The first profile PR only needs enough structure to support profile-aware graph validation and viewer palette filtering later.

## InterfaceRegistry

Loaded profiles are registered as:

```text
profile.<profile_id>
```


### Selecting an admitted Pack revision

A signed Normal Sandbox or declarative Application release may retain the same
Pack ID while advancing its version. Native onboarding verifies the separately
selected publisher key and the exact preview, including the predecessor and
catalog revision. Admission retains old signed CAS artifacts; it does not switch
a running Profile. Host Extension, Base and Shell artifacts retain their own
Host trust and update boundaries.

The Launcher Profile view lists retained versions for Packs explicitly declared
in the selected Profile definition. Saving a choice appends an immutable
definition successor using the definition revision, store generation, selected
digest and admission catalog revision as CAS preconditions. The ordinary
resolve, review, approval and activation ceremony remains required. The old
active Profile, Plan and CAS roots remain available for rollback. Unknown pins,
conflicting versions, modified signed content and stale selections fail closed.

Packs enabled through the existing optional `pack.enable` path are not promoted
to mandatory definition rows by this selector. Optional revision pin persistence
and Host-to-Host development-state handover are separate, unfinished work.
Development Launchers still isolate state by executable digest; building a new
Launcher does not migrate a running VM or authorize reuse of its credentials.

### Model output limits

The public model configuration route accepts a positive integer
`max_output_tokens` (1–131072). The model registry stores it as `max_tokens` and
the Gateway applies the smaller of the saved limit and explicit caller limits
before Provider dispatch. Caller output-limit aliases cannot raise the ceiling;
multiple completions are rejected on saved-model routes. Revisioned updates
preserve existing model requirements and private metadata. The UI displays the
finite ceiling without exposing those private fields.

This is a per-response output limit. It is not an aggregate monetary budget:
tool continuations can produce further responses, and input tokens also cost
money. Strategy reservations remain conservative upper bounds when a saved
limit is narrower than their explicit requested ceiling.
