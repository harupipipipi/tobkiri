import test from "node:test";
import assert from "node:assert/strict";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { userFacingModelProfiles, profileNeedsApiKey } from "../../App";
import {
  ModelRouteErrorNotices,
  ModelRouteSetup,
  ProviderReadiness,
} from "./ModelRouteSetup";

import {
  buildVisibleModelOptions,
  enrichModelSelectOptions,
  findSelectedModelOption,
  filterModelOptionsByProvider,
  filterModelProviderOptions,
  modelOptionBadges,
  modelOptionNeedsVisionRecommendation,
  modelOptionThinkingLevels,
  modelProviderOptions,
  modelSearchItemToModelSelectOption,
  parseModelProviderQuery,
  parseModelAllowlist,
  serializeModelAllowlist,
  type ModelSelectOption,
} from "./modelSelect";
import {
  SettingsModelSearchField,
  withThinkingLevelForModel,
} from "../../renderers/settings/renderers/modelSelectField";

test("saved canonical routes remain selectable without claiming Provider health", () => {
  const route = {
    profile_id: "daily", display_name: "Daily", model_id: "deepseek-chat",
    provider_id: "provider.deepseek.main", route_configured: true,
  };
  assert.deepEqual(userFacingModelProfiles([route], "stub/default"), [route]);
  assert.equal(profileNeedsApiKey(route), false);
  assert.equal("availability" in route, false);
  assert.deepEqual(userFacingModelProfiles([{ ...route, route_configured: false }], "stub/default"), []);
  assert.deepEqual(userFacingModelProfiles([{ ...route, type: "embedding" }], "stub/default"), []);
});

test("model route setup never offers a free-form provider connection ID", () => {
  const html = renderToStaticMarkup(createElement(ModelRouteSetup));

  assert.match(html, /Provider接続ID（登録済みのみ）/);
  assert.match(html, /aria-label="Provider connection ID"/);
  assert.match(html, /<select/);
  assert.match(html, /登録済みの接続がありません/);
  assert.doesNotMatch(html, /provider\.deepseek\.main/);
});

test("model route setup errors keep severity icons separate from stable copy actions", () => {
  const html = renderToStaticMarkup(createElement(ModelRouteErrorNotices, {
    connectionsError: "接続一覧を取得できませんでした。",
    saveError: "保存時に接続が更新されました。",
  }));

  assert.match(html, /data-error-notice="model-route-provider-connections"/);
  assert.match(html, /data-error-icon="model-route-provider-connections"/);
  assert.match(html, /aria-label="Provider接続一覧エラーをコピー"/);
  assert.match(html, /data-error-notice="model-route-save"/);
  assert.match(html, /data-error-icon="model-route-save"/);
  assert.match(html, /aria-label="モデルルート保存エラーをコピー"/);
  assert.match(html, /data-copy-icon=""/);
});

test("provider readiness distinguishes credential, health, and reachability", () => {
  const html = renderToStaticMarkup(createElement(ProviderReadiness, {
    connection: {
      provider_instance_id: "provider.example",
      display_name: "Example",
      credential_status: "missing",
      health_status: "unverified",
      reachability: "unknown",
      observed_at: null,
    },
  }));

  assert.match(html, /資格情報: 未設定/);
  assert.match(html, /到達性: 未確認/);
  assert.match(html, /未検証/);
});

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

test("model selector uses declared thinking levels and only recommends Vision for known text-only models", () => {
  const textOnly: ModelSelectOption = {
    value: "demo/text",
    label: "Text model",
    supports_vision: false,
    supports_image_input: false,
    supports_thinking: true,
    thinking_levels: ["none", "low", "high"],
  };

  assert.deepEqual(modelOptionThinkingLevels(textOnly), ["none", "low", "high"]);
  assert.equal(modelOptionNeedsVisionRecommendation(textOnly, textOnly.value), true);
  assert.equal(modelOptionNeedsVisionRecommendation({ value: "unknown", label: "Unknown" }, "unknown"), false);
  assert.equal(modelOptionNeedsVisionRecommendation({ ...textOnly, supports_image_input: true }, textOnly.value), false);
});

test("settings options recover model capability metadata from the Host-read profile catalog", () => {
  const [option] = enrichModelSelectOptions(
    [{ value: "google/gemini-3-pro-preview", label: "Gemini 3 Pro" }],
    [{
      profile_id: "google/gemini-3-pro-preview",
      display_name: "Gemini 3 Pro",
      provider_id: "google",
      model_id: "gemini-3-pro-preview",
      supports_thinking: true,
      thinking_levels: ["low", "high"],
      default_thinking_level: "high",
      supports_vision: true,
    }],
  );

  assert.equal(option.provider_id, "google");
  assert.deepEqual(modelOptionThinkingLevels(option), ["low", "high"]);
  assert.equal(option.default_thinking_level, "high");
  assert.equal(option.supports_vision, true);
});

test("settings model selector keeps thinking next to the selected model", () => {
  const html = renderToStaticMarkup(createElement(SettingsModelSearchField, {
    value: "demo/text",
    options: [{
      value: "demo/text",
      label: "Text model",
      supports_vision: false,
      supports_image_input: false,
      supports_thinking: true,
      thinking_levels: ["low", "high"],
      default_thinking_level: "high",
    }],
    thinkingLevelsByProfile: { "demo/text": "low" },
    recommendVision: true,
    onChange: () => undefined,
    onThinkingLevelChange: () => undefined,
  }));

  assert.match(html, /data-settings-model-thinking/);
  assert.match(html, /aria-label="考える深さ"/);
  assert.match(html, /Vision対応モデルを推奨/);
  assert.match(html, /<option value="low" selected="">Low<\/option>/);
});

test("remote model capability metadata persists its explicitly selected thinking level", () => {
  const remoteOption: ModelSelectOption = {
    value: "remote/reasoner",
    label: "Remote Reasoner",
    supports_thinking: true,
    thinking_levels: ["low", "high"],
  };

  assert.deepEqual(
    withThinkingLevelForModel({}, remoteOption.value, remoteOption, "high"),
    { "remote/reasoner": "high" },
  );
  assert.equal(
    withThinkingLevelForModel({}, remoteOption.value, remoteOption, "medium"),
    null,
  );
});

test("remembered server-selected options remain visible after a blank-query reset", () => {
  const selected: ModelSelectOption = {
    value: "remote/reasoner",
    label: "Remote Reasoner",
    provider_id: "remote",
    supports_thinking: true,
    thinking_levels: ["high"],
  };

  assert.deepEqual(
    buildVisibleModelOptions({ options: [selected], remoteOptions: [], query: "" }),
    [selected],
  );
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
