# Tobkiri Subagent Placement Pack

Compiles Pack-declared Subagent Definitions and Placements into immutable,
revision-pinned Effective Subagent Plans.

The compiler reuses the selected Global Contract Registry and an existing
CapabilityPlan. Authority is the intersection of every non-empty allow layer,
denials are cumulative, budgets use the smallest limit, approval uses the
strictest level, and compiler stages cannot widen authority.

The product vocabulary remains Main Agent, subagent, Placement, and Team.
Pack-defined protocols and Placement features stay outside Core enums.

## Complete v1 topology

- A Subagent Definition declares reusable execution capability.
- A Placement selects role, model, Tools, Skills, Memory, Workspace,
  participation protocols, governance, and enforcement.
- A Placement Map contains exactly one logical Main Agent, its Subagents,
  Team-wide bindings, and public ports.
- The topology compiler constrains every member plan by Team governance and
  pins the resulting map, plans, registry revision, and topology hash.
- Runtime Assignments pin new runs to one exact Effective Plan. Placement
  patches create a new desired revision; existing runs do not silently migrate.
- Remote Agent Cards can be adapted as opaque `remote_agent` definitions.
  Remote attestation is never represented as host enforcement.
- A compiled Team can be exported as one `composite_team` Subagent without
  exposing its internal members.

Defaultspack projects its former `DEFAULT_AGENT_SPECS` records from canonical
built-in Placements. The legacy fields remain readable during migration, but
Agent execution now verifies and persists `placement_id`,
`placement_revision`, `placement_map_id`, protocol membership, root scope, and
`effective_plan_hash`.
