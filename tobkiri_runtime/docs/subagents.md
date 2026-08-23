# Delegation Compatibility

Rumi no longer treats "subagent" as a primary architecture concept.

For user-facing wording, prefer:

- `team workspace` for the long-running multi-agent workspace surface
- `team` for the cooperating set of agents inside that workspace
- `delegation` for sending bounded work to another agent
- `specialist` or `delegated agent` for a narrowly scoped worker role

`company` and `subagent` remain compatibility/internal names where older APIs,
routes, stored identifiers, or docs still use them.

The canonical runtime contract is:

- `chat.message`: normal conversation input
- `run.instruction`: queued steering or runtime guidance
- `run.interrupt`: urgent runtime guidance
- `agent.delegate`: one delegated tool-capable run
- `model.call`: one bounded model-to-model question with no tools by default
- `model.switch`: persistent conversation model change
- `model.route`: turn-scoped routing override

`subagent` remains as a compatibility name and user-facing alias for older
routes, functions, tools, labels, and docs that still refer to delegated work.

## Current Boundary

- `agent.delegate` = one delegated run that may use tools, approvals, and normal runtime policy
- `multi-agent` = coordinated group execution across more than one delegated worker
- utility roles such as `tool_selector`, `prompt_compactor`, `context_summarizer`, `model_router`, and `vision_ocr` are implemented through `model.call`-style utility routing rather than a special subagent framework

## Compatibility Paths

These compatibility surfaces remain available:

- `/api/agent/subagent`
- `defaults.agent.run_subagent`
- `defaultspack.agent.run_subagent`
- `defaults.tool.subagent`
- `defaultspack.tool.subagent`
- the `subagent` child-conversation tool in `rumi_default_tools_pack`

They are kept for backward compatibility and should route through the shared input,
model, tool, and policy contracts instead of introducing parallel behavior.

In practice that means:

- utility-role compatibility calls route through shared `model.call`-style utility routing
- task-like compatibility calls route through the common input dispatcher as `agent.delegate`

Where older docs say `company workspace`, read that as today's `team workspace`
unless the text is specifically describing a compatibility API or stored runtime
identifier.

## Policy and Approval

Using a compatibility `subagent` alias does not bypass:

- tool policy
- approval gates
- runtime profile tool connectivity
- model capability checks
- workspace trust requirements

If delegated work needs tools, it should use the same policy and approval path as any
other run.

For delegated coding in independent Git worktrees, use the normalized admission,
attempt-budget, evidence, and handoff contract in
[`worktree-team-contract.md`](worktree-team-contract.md). It applies equally to
native and external-harness workers and keeps candidate evidence distinct from
reviewed, stable, and final output.
