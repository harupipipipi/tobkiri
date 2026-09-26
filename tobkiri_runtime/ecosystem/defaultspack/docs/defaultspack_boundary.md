# Defaultspack Boundary

defaultspack is Rumi's core runtime pack. It provides the common execution
surface for packs and user data, but it is not a collection of concrete tools,
agent products, prompts, UI entries, or model catalogs.

## Belongs In Defaultspack

- Runtime, broker, registry loader, adapter, and transport code.
- Capability contracts, schema vocabulary, and generic execution bridges.
- Pack/user_data loaders for tools, prompts, profiles, presets, UI manifests,
  provider catalogs, and capabilities.
- Core management functions such as module listing, pack requests, and policy
  review.
- Minimal startup graphs needed to connect the runtime.

## Belongs In Packs Or User Data

- AI-facing tool definitions and concrete tool implementations.
- Agent behavior prompts, profiles, presets, examples, and product-specific
  graphs.
- Product profiles such as Operations Company.
- Sidebar items, settings sections, renderers, app shell variants, and other
  concrete frontend declarations.
- Provider and model catalog data.

## Starter Packs

The default local experience is assembled from defaultspack plus starter packs:

- `rumi_default_tools_pack`: default tool manifests and tool functions.
- `rumi_local_agent_pack`: local agent prompts, profiles, presets, and examples.
- `rumi_operations_team_pack`: Operations Company profile, graph, routes, and UI.
- `rumi_reference_ui_pack`: reference sidebar and panel manifests.
- `rumi_model_catalog_pack`: provider/model catalog manifests and provider UI.

Loaders must aggregate installed packs and `user_data` and attach
`source_pack_id` when possible. defaultspack may keep deprecated compatibility
aliases, but new concrete content should land in a pack or user data.
