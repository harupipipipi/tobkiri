# Team Workspace

This document uses `team workspace` as the user-facing name for the long-running
coordination surface built on top of defaultspack primitives. Internal ids,
routes, and pack paths may still use `company` for compatibility. The
product-specific Operations Company profile lives in
`ecosystem/rumi_operations_team_pack/`; defaultspack owns the generic
contracts it uses: chat storage, agent runtime, scheduler, memory, compact
context, tool policy, approval, audit, and workspace-scoped coding.

The workspace is one user-facing conversation with internal team roles behind
it. A client manager keeps the external thread coherent while project manager,
coding engineer, research specialist, reviewer, operations monitor, and
scheduler roles coordinate through internal state and channels.

## Ownership Boundary

The team workspace, worker runtime, and scheduled agent runtime are
model-independent defaultspack infrastructure. They own durable coordination
state, routing, execution handoff, scheduling, approval, audit, and workspace
policy. They must not be described as a MiMo-only company or coding runtime.

`ecosystem/rumi_operations_team_pack/` owns the optional MiMo Coding
Company profile and harness. Concrete model/provider names, including MiMo
model aliases and OpenCode Zen integrations, belong there or in provider
configuration. Compatibility modules, ids, and routes may still use `company`;
those transport names do not make the shared runtime MiMo-specific.

## Runtime Shape

- The team workspace profile is optional and pack-owned, not a defaultspack
  startup requirement.
- Bootstrap creates or reuses one organization, one operations conversation, and
  an interval heartbeat schedule when nonstop mode is requested.
- Roles are least-privilege by default. Each role receives only the tools needed
  for its job.
- Model self-selection is allowlist-based and audit-reasoned.
- Long-running memory prefers decisions, incidents, handoffs, compact summaries,
  and schedule state over unbounded transcript growth.
- Delegated work between roles should be described as `delegation`. Older
  `subagent` wording, where it appears elsewhere, is compatibility language
  around the same idea rather than a separate architecture layer.

## Security Invariants

- Local policy is authoritative. Team roles cannot bypass defaultspack tool
  policy, approval gates, route guards, or audit.
- File writes, destructive file operations, terminal execution, git commit/push,
  external sends, settings mutation, and credentials still require the normal
  local approval path when policy marks them sensitive.
- Remote peers and external systems can submit messages only through normalized
  input paths. They cannot grant approval, install packs, mutate settings, or
  issue local approval tokens.
- Coding work remains confined to the active workspace root. Team roles may ask
  for code changes, but file, terminal, and git handlers enforce workspace
  boundaries.
- P2P, if enabled by a future pack or local setting, is message ingress only. It
  does not expose tool execution or approval authority. See
  `p2p_security.md`.

## Operational Guidance

Use the team workspace for sustained monitoring, scheduled work, and multi-role
execution where the user still wants one clear client-facing thread. Keep
normal status silent, escalate incidents with evidence, and summarize internal
progress before asking the user for authority, credentials, or judgment.
