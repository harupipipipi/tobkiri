import test from "node:test";
import assert from "node:assert/strict";

import type { ModelProfile, UICatalog } from "../../lib/api";
import type { ModelSelectOption } from "./modelSelect";
import {
  filterModelOptionsBySelector,
  filterModelProfilesBySelector,
  filterProvidersBySelector,
  modelSelectorSchemaForSurface,
  modelSelectorSchemaFromCatalog,
  parseModelSelectorSchema,
} from "./modelSelectorSchema";

function profile(profileId: string, providerId: string, tags: string[] = []): ModelProfile {
  return {
    profile_id: profileId,
    display_name: profileId,
    provider_id: providerId,
    model_id: profileId.split("/").at(-1),
    qualified_model_id: profileId,
    capability_tags: tags,
    availability: { configured: true, available: true },
  };
}

test("catalog metadata is the single selector schema source", () => {
  const catalog = {
    templates: [{
      id: "rumi.model_selector.default",
      metadata: {
        selector_schema: {
          layout: { max_visible_options: 24 },
          filters: { exclude_model_ids: ["openrouter/legacy/*"] },
        },
      },
    }],
  } as unknown as UICatalog;

  const schema = modelSelectorSchemaFromCatalog(catalog);
  assert.equal(schema.layout.max_visible_options, 24);
  assert.deepEqual(schema.filters.exclude_model_ids, ["openrouter/legacy/*"]);
});

test("model ID, provider and tag filters apply to composer profiles", () => {
  const schema = parseModelSelectorSchema({
    filters: {
      exclude_model_ids: ["openrouter/legacy/*"],
      exclude_provider_ids: ["blocked-*"],
      exclude_tags: ["deprecated"],
      require_tags: ["tools"],
      require_tag_mode: "all",
    },
  });
  const profiles = [
    profile("openrouter/good/chat", "openrouter", ["tools"]),
    profile("openrouter/legacy/chat", "openrouter", ["tools"]),
    profile("blocked-cloud/chat", "blocked-cloud", ["tools"]),
    profile("openrouter/old/chat", "openrouter", ["tools", "deprecated"]),
    profile("openrouter/plain/chat", "openrouter"),
  ];

  assert.deepEqual(
    filterModelProfilesBySelector(profiles, schema, "composer").map((item) => item.profile_id),
    ["openrouter/good/chat"],
  );
});

test("the same model exclusion applies to settings options", () => {
  const schema = parseModelSelectorSchema({
    filters: { exclude_model_ids: ["vendor/hidden", "*/experimental/*"] },
  });
  const options: ModelSelectOption[] = [
    { value: "vendor/stable", label: "Stable", provider_id: "vendor" },
    { value: "vendor/hidden", label: "Hidden", provider_id: "vendor" },
    { value: "vendor/experimental/chat", label: "Experimental", provider_id: "vendor" },
  ];

  assert.deepEqual(
    filterModelOptionsBySelector(options, schema, "settings").map((item) => item.value),
    ["vendor/stable"],
  );
});

test("settings options preserve availability for hide-unavailable policy", () => {
  const schema = parseModelSelectorSchema({
    filters: { hide_unavailable: true },
  });
  const options: ModelSelectOption[] = [
    { value: "vendor/ready", label: "Ready", provider_id: "vendor" },
    {
      value: "vendor/unsupported",
      label: "Unsupported",
      provider_id: "vendor",
      availability: { status: "unavailable" },
    },
  ];

  assert.deepEqual(
    filterModelOptionsBySelector(options, schema, "settings").map((item) => item.value),
    ["vendor/ready"],
  );
});

test("surface overrides preserve global layout and filters", () => {
  const schema = parseModelSelectorSchema({
    layout: {
      group_by: "none",
      max_visible_options: 25,
      show_search: false,
      trigger_height_px: 52,
      popover_width_px: 560,
      popover_max_height_px: 360,
    },
    filters: { exclude_tags: ["deprecated"] },
    surfaces: {
      composer: {
        layout: { placement: "above" },
        filters: { exclude_model_ids: ["vendor/private"] },
      },
    },
  });
  const composer = modelSelectorSchemaForSurface(schema, "composer");

  assert.equal(composer.layout.placement, "above");
  assert.equal(composer.layout.group_by, "none");
  assert.equal(composer.layout.max_visible_options, 25);
  assert.equal(composer.layout.show_search, false);
  assert.equal(composer.layout.trigger_height_px, 52);
  assert.equal(composer.layout.popover_width_px, 560);
  assert.equal(composer.layout.popover_max_height_px, 360);
  assert.deepEqual(composer.filters.exclude_tags, ["deprecated"]);
  assert.deepEqual(composer.filters.exclude_model_ids, ["vendor/private"]);
});

test("provider choices use the shared include/exclude policy", () => {
  const schema = parseModelSelectorSchema({
    filters: {
      include_provider_ids: ["open*", "local"],
      exclude_provider_ids: ["openai-legacy"],
    },
  });
  const providers = [
    { provider_id: "openrouter" },
    { provider_id: "openai-legacy" },
    { provider_id: "local" },
    { provider_id: "google" },
  ];

  assert.deepEqual(
    filterProvidersBySelector(providers, schema, "settings").map((item) => item.provider_id),
    ["openrouter", "local"],
  );
});
