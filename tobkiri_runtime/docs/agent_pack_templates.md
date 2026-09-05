# Agent Pack templates

`tobkiri-pack` creates production-oriented Pack skeletons instead of loose
prompt examples. Generated Packs use the current Activity v1, Skill v2, Tool
v3, and function contracts, validate immediately, and begin with no granted
authority.

## Create a Pack

Run the CLI from `tobkiri_runtime`:

```bash
python scripts/tobkiri_pack.py init ./my-pack \
  --pack-id example.agent \
  --display-name "Example Agent" \
  --profile complete
```

Available profiles are:

- `minimal`: Pack metadata and authoring guidance only.
- `codex`: repository-first instructions, scoped `AGENTS.md`, patches, tests,
  and diff review.
- `hermes`: narrow toolset and backend selection with opt-in memory and
  reviewable learned procedures.
- `complete`: the compatible Codex and Hermes strengths in one template.
- `auto`: lets the template resolver choose `codex`, `hermes`, or `complete`
  from `--intent`; an explicit profile always wins.

For example:

```bash
python scripts/tobkiri_pack.py init ./repo-agent \
  --pack-id example.repo_agent \
  --display-name "Repository Agent" \
  --profile auto \
  --intent "Review a repository, create patches, and verify pull requests"
```

## Generated contract

A non-minimal profile contains:

- `AGENTS.md` for hierarchical, subtree-scoped operating instructions.
- `template.contract.json` for AI selection, progressive schemas, immutable
  Capability Plans, backend/model routing, and opt-in learning rules.
- an Activity manifest describing the user-facing task.
- a procedural Skill with compatibility guidance.
- a Tool v3 manifest preserving input/output, execution, effects, risk,
  approval, discovery, requirements, security, and UI metadata.
- a pure function example with no network or filesystem authority.

The sample Tool is read-only and sandbox-required. A separately added Tool is
disabled and denied until its function target, tests, permissions, and review
are complete. Templates never grant host capability, approval, publisher
trust, secrets, or memory access.

## Add a component

```bash
python scripts/tobkiri_pack.py add ./my-pack activity \
  --id example.agent.review \
  --display-name "Review" \
  --description "Review a bounded change"

python scripts/tobkiri_pack.py add ./my-pack skill \
  --id example.agent.review_procedure \
  --display-name "Review procedure" \
  --description "Apply the project's review procedure"

python scripts/tobkiri_pack.py add ./my-pack tool \
  --id example.agent.read_context \
  --display-name "Read context" \
  --description "Read approved task context"
```

The command refuses to overwrite existing files and validates every generated
manifest against its authoritative schema.

## Skill or Tool?

Use a Skill when the agent can complete the task with instructions and
existing capabilities. Add a Tool only for a new API, executable, binary,
stream, or custom execution boundary. This keeps procedural knowledge easy to
review while keeping executable authority explicit.

AI selection is supported, but never exclusive: explicit user selection wins.
The AI ranks relevant Activities, Skills, and Tools, loads schemas
progressively, and compiles the exact choices into an immutable Capability
Plan before privileged execution.

## Learning and memory

Memory and session search are opt-in. Feedback may produce a disabled Skill
draft for human review; it cannot silently alter an enabled Skill, grant
permissions, or turn retrieved content into instructions.

## Release checklist

Before distribution:

1. Replace placeholders and delete unused components.
2. Add contract, policy, negative-path, and function tests.
3. Inspect the exact requested capabilities and approval rules.
4. Validate the Pack, review its complete diff, and sign the reviewed
   artifact.
5. Install through the Host trust flow; a signature proves integrity and
   publisher identity, not authority.
