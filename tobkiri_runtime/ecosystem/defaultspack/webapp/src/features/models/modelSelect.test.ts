import test from "node:test";
import assert from "node:assert/strict";

import {
  buildVisibleModelOptions,
  findSelectedModelOption,
  filterModelOptionsByProvider,
  filterModelProviderOptions,
  modelOptionBadges,
  modelProviderOptions,
  modelSearchItemToModelSelectOption,
  parseModelProviderQuery,
  parseModelAllowlist,
  serializeModelAllowlist,
  type ModelSelectOption,
} from "./modelSelect";

function makeModelOption(index: number): ModelSelectOption {
  return {
    value: `demo/provider-model-${index}`,
    label: `Demo Provider / Model ${index}`,
    provider_id: "demo",
    provider_display_name: "Demo Provider",
    model_id: `model-${index}`,
  };
}

test("buildVisibleModelOptions keeps configured models beyond the old first-40 cutoff", () => {
  const filler = Array.from({ length: 45 }, (_, index) => makeModelOption(index));
  const configuredOption: ModelSelectOption = {
    value: "opencode-zen/minimax-m3-free",
    label: "OpenCode Zen / MiniMax M3 Free via OpenCode Zen",
    provider_id: "opencode-zen",
    provider_display_name: "OpenCode Zen",
    model_id: "minimax-m3-free",
    configured: true,
    supports_tool_calling: true,
    supports_thinking: true,
    supports_vision: true,
  };

  const visible = buildVisibleModelOptions({
    options: [...filler, configuredOption],
    selected: null,
    remoteOptions: [],
    query: "",
  });

  assert.equal(visible.length, 46);
  assert(visible.some((option) => option.value === configuredOption.value));
});

test("buildVisibleModelOptions searches provider, model, notes, and remote options without duplicates", () => {
  const local = [
    { ...makeModelOption(1), notes: "fast balanced coding" },
    { ...makeModelOption(2), provider_id: "other", provider_display_name: "Other Provider" },
  ];
  const selected = { value: "selected/model", label: "Selected Model" };
  const remote = [
    { ...selected, label: "Selected Remote Duplicate" },
    {
      value: "google/gemini-2.5-pro",
      label: "Gemini 2.5 Pro",
      provider_id: "google",
      model_id: "gemini-2.5-pro",
    },
  ];

  const visible = buildVisibleModelOptions({
    options: local,
    selected,
    remoteOptions: remote,
    query: "gemini pro",
  });

  assert.deepEqual(visible.map((option) => option.value), [
    "selected/model",
    "google/gemini-2.5-pro",
  ]);
});

test("model search items map API key and capability status into select options", () => {
  const option = modelSearchItemToModelSelectOption({
    profile_id: "openai/gpt-4.1",
    display_name: "GPT 4.1",
    provider_id: "openai",
    provider_display_name: "OpenAI",
    model_id: "gpt-4.1",
    requires_api_key: true,
    api_key_configured: false,
    supports_tool_calling: true,
  });

  assert.equal(option.value, "openai/gpt-4.1");
  assert.equal(option.requires_api_key, true);
  assert.equal(option.api_key_configured, false);
  assert.deepEqual(modelOptionBadges(option).map((badge) => badge.id), ["api-key-needed", "tools"]);
});

test("findSelectedModelOption falls back to the raw value", () => {
  assert.deepEqual(findSelectedModelOption([], "custom/model"), {
    value: "custom/model",
    label: "custom/model",
  });
});

test("model allowlist parsing and serialization dedupe stable model ids", () => {
  const parsed = parseModelAllowlist("stub/default, google/gemini\nstub/default");

  assert.deepEqual(parsed, ["stub/default", "google/gemini"]);
  assert.equal(serializeModelAllowlist(parsed), "stub/default\ngoogle/gemini");
});

test("@provider query offers providers and scopes the following model search", () => {
  const options: ModelSelectOption[] = [
    {
      value: "openai/gpt-4.1",
      label: "GPT 4.1",
      provider_id: "openai",
      provider_display_name: "OpenAI",
    },
    {
      value: "openrouter/anthropic/claude-sonnet-4",
      label: "Claude Sonnet 4",
      provider_id: "openrouter",
      provider_display_name: "OpenRouter",
    },
    {
      value: "openrouter/google/gemini-2.5-pro",
      label: "Gemini 2.5 Pro",
      provider_id: "openrouter",
      provider_display_name: "OpenRouter",
    },
  ];
  const providers = modelProviderOptions(options);

  assert.deepEqual(
    filterModelProviderOptions(providers, "router").map((provider) => provider.provider_id),
    ["openrouter"],
  );
  assert.deepEqual(parseModelProviderQuery("@openr", providers), {
    active: true,
    providerQuery: "openr",
    providerId: "",
    modelQuery: "",
  });
  assert.deepEqual(parseModelProviderQuery("@openrouter gemini", providers), {
    active: false,
    providerQuery: "openrouter",
    providerId: "openrouter",
    modelQuery: "gemini",
  });
  assert.deepEqual(
    filterModelOptionsByProvider(options, "openrouter").map((option) => option.value),
    [
      "openrouter/anthropic/claude-sonnet-4",
      "openrouter/google/gemini-2.5-pro",
    ],
  );
});
