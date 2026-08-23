# Prompt Design

Prompt is a passive text layer. It stores, validates, resolves, and renders
prompt templates, but it does not select tools, grant permissions, choose AI
providers, call models, or mutate chat state by itself.

## Effective Prompt Priority

`defaults.prompt.load_effective` and
`defaults.prompt.resolve_for_conversation` use the same priority:

1. Profile override from the workspace prompt directory
   `profiles/<profile_id>/prompts/`.
2. Profile snapshot from
   `profiles/<profile_id>/ecosystem/snapshots/<pack>/prompts/`.
3. Pack default from defaultspack prompt components or prompt extensions.

The workspace prompt file is the formal `profile_override` layer. It is
user-owned and wins over snapshots. Every effective prompt response includes
`source_type`, `source`, `source_chain`, `content`, and `final_content` so flow
steps can audit which layer produced the final text.

## Pack Trust Boundary

Pack-provided prompts are model-visible only when their source pack is approved
and hash-verified by the host approval manager. Untrusted pack prompts are
skipped by prompt listing/resolution. The AI Input Graph also fails closed:
if a pack-sourced prompt reaches runtime from an older path or test hook, its
edge is inactive and the disabled segment records
`prompt_source_pack_untrusted`.

The shipped prompt-only trusted set is intentionally small:
`defaultspack`, `rumi_default_tools_pack`, and
`rumi_operations_team_pack`. Extension and component prompt manifests must
also resolve from inside the claimed shipped pack root, so a manifest cannot
gain trust only by spoofing `source_pack_id`. This trust only allows passive
prompt text to enter model input; it does not grant tool, provider, filesystem,
terminal, browser, or chat-state authority.

User-owned profile overrides remain editable prompt text, but they do not grant
permissions, attach tools, call providers, or mutate chat state. Trusting a pack
allows its prompt text to be considered as passive model input; it still does
not grant execution authority.

## Functions

- `defaults.prompt.load_effective` returns the selected prompt text and source
  chain without rendering conversation variables.
- `defaults.prompt.resolve_for_conversation` resolves the same effective prompt
  and renders `{{...}}` variables from explicit `variables` plus passive
  `context.*` values such as `context.profile_id`, `context.conversation_id`,
  `context.message_count`, and `context.messages`.
- `defaults.prompt.validate_template` validates template syntax and reports user
  variables, context variables, declared variables, warnings, and errors.
- `defaults.prompt.render` renders an explicit prompt/template with supplied
  variables.

## Authoring Rules

Prompt templates may use `{{variable}}` and `{{context.variable}}` placeholders.
Missing variables are left in the text by the renderer; validation can be used to
detect them before a flow runs.

Prompt authoring must not create executable tools. `execution.type="prompt"` is
a legacy compatibility path only and is not an authoring surface. If a workflow
needs rendered prompt text, call `defaults.prompt.render` from a flow/function.
If a tool is needed, author a `rumi_function` or `capability` tool facade.

Prompt files are data. Python prompt hooks that read files, call providers, or
touch host capabilities do not belong in prompt authoring; that logic must live
behind trusted functions and explicit capability grants.

Prompt visibility, active graph toggles, editor overrides, diffs, versions, and
chat response trace inspection are documented in
[prompt-workspace.md](prompt-workspace.md).
