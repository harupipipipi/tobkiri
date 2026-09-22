# Defaultspack Functions

Defaultspack exposes its default capabilities as Rumi functions. HTTP routes, AI tools, and Flow nodes should treat functions as the stable public operation contract.

## Calling A Function

Use the canonical qualified name when you know it:

```json
{
  "type": "function.call",
  "qualified_name": "defaultspack:ai_set_thinking_level",
  "args": {
    "scope": "profile",
    "profile_id": "openrouter/tencent/hy3-preview:free",
    "level": "high"
  }
}
```

Functions also publish vocabulary aliases such as `defaults.ai.set_thinking_level` and `defaultspack.ai.set_thinking_level`. The canonical function id never contains dots; aliases do.

## Function Vs Tool

A function is the runtime/API operation. A tool is only the facade shown to an AI model.

```json
{
  "tool_id": "set_thinking_level",
  "name": "set_thinking_level",
  "execution": {
    "type": "rumi_function",
    "qualified_name": "defaultspack:ai_set_thinking_level"
  }
}
```

`ToolExecutor` sends `rumi_function` calls through the shared `CapabilityExecutor`, so tool use and pack-to-pack calls pass through the same permission boundary.

## Thinking Level

Model runtime settings are owned by `ModelRuntimeSettingsService`. The main entrypoints are:

- `defaultspack:ai_get_preferred_model`
- `defaultspack:ai_set_preferred_model`
- `defaultspack:ai_get_thinking_level`
- `defaultspack:ai_set_thinking_level`
- `defaultspack:ai_get_effective_thinking_level`
- `defaultspack:ai_normalize_thinking_level`

When chat or AI completion params do not include `thinking_level`, defaultspack resolves the effective level server-side from conversation, profile, then global settings.

`thinking_level` controls the per-response reasoning depth for the active model/profile. It does not enable or change the DeepThink review loop or its section budget. `deepthink_enabled` enables the longer multi-step review flow; `deepthink_max_review_iterations` and `deepthink_max_sections` limit that flow independently and are not `thinking_level`.

## Model Capabilities And Routing

The model catalog now exposes capability metadata used by profile-aware routing:

- `defaultspack:ai_search_models` / `defaults.ai.search_models`
- `defaultspack:ai_get_model_capabilities` / `defaults.ai.get_model_capabilities`
- `defaultspack:ai_recommend_model` / `defaults.ai.recommend_model`
- `defaultspack:ai_route_model` / `defaults.ai.route_model`
- `defaultspack:ai_explain_model_choice` / `defaults.ai.explain_model_choice`

Capability fields include `supports_vision`, `supports_tool_calling`, `supports_thinking`, `supports_fast`, `speed_tier`, `quality_tier`, `knowledge_level`, `knowledge_band`, and role recommendations. `knowledge_level` is a relative rumiai routing score, not an absolute claim about intelligence.

Vision bridge and compatibility utility routing are available through:

- `defaultspack:vision_describe_images` / `defaults.vision.describe_images`
- `defaultspack:agent_run_subagent` / `defaults.agent.run_subagent` (compatibility alias for utility routing or delegated runs)
- `defaultspack:prompt_lint_prompt` / `defaults.prompt.lint_prompt`
- `defaultspack:prompt_compact_prompt` / `defaults.prompt.compact_prompt`

Prompt Workspace management is available through:

- `defaultspack:prompt_active` / `defaults.prompt.active`
- `defaultspack:prompt_trace_list` / `defaults.prompt.trace_list`
- `defaultspack:prompt_trace_get` / `defaults.prompt.trace_get`
- `defaultspack:prompt_toggle` / `defaults.prompt.toggle`
- `defaultspack:prompt_preview_toggle` / `defaults.prompt.preview_toggle`
- `defaultspack:prompt_editor_load` / `defaults.prompt.editor_load`
- `defaultspack:prompt_editor_save` / `defaults.prompt.editor_save`
- `defaultspack:prompt_create_override` / `defaults.prompt.create_override`
- `defaultspack:prompt_test` / `defaults.prompt.test`
- `defaultspack:prompt_diff` / `defaults.prompt.diff`
- `defaultspack:prompt_versions` / `defaults.prompt.versions`
- `defaultspack:prompt_rollback` / `defaults.prompt.rollback`

See [prompt_workspace.md](prompt_workspace.md) for chat trace inspection,
override priority, `disabled_edges`, and the passive prompt safety boundary.
Prompt trace detail is redacted unless `include_text=true` is explicit, and
all `/api/prompts/*` HTTP routes are local-only sensitive routes. Prompt Studio
saves, overrides, and rollbacks require `expected_body_hash` or
`expected_exists=false`; unconditional prompt writes are rejected with
`PROMPT_WRITE_CONFLICT`. Versioned writes use atomic replacement plus rollback
compensation to avoid phantom versions and unaudited rollback deletes.

## Flow Example

```yaml
- id: set_reasoning
  phase: prepare
  priority: 10
  type: function
  function: defaultspack.ai.set_thinking_level
  input:
    scope: turn
    level: high
  output: thinking_level_result
```

## Security

Read/list/search/status functions are low risk. Mutating chat, AI invocation, memory, and artifacts are usually medium risk. File writes, terminal execution, git push/commit, provider key changes, browser/computer control, clipboard writes, and forced pack patch operations are high risk and declare `caller_requires`.

Pack authors should call defaultspack functions through `ToolExecutor` or the shared `CapabilityExecutor` so the caller principal is preserved. `domain.function_runtime.bridge.invoke_function()` defaults to the internal `defaultspack` principal for HTTP route adapters and other defaultspack-owned fallbacks; external packs that call it directly must pass an explicit `principal_id`.
