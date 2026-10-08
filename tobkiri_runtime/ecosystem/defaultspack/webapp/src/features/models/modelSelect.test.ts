import test from "node:test";
import assert from "node:assert/strict";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { userFacingModelProfiles, profileNeedsApiKey } from "../../App";
import { ModelSearchPicker } from "./ModelSearchPicker";
import { DEFAULT_MODEL_SELECTOR_SCHEMA } from "./modelSelectorSchema";
import {
  ModelRouteErrorNotices,
  ModelRouteSetup,
  CatalogModelPicker,
  ProviderReadiness,
  modelRouteConnectionsErrorMessage,
  modelRouteSaveErrorMessage,
} from "./ModelRouteSetup";

import {
  buildVisibleModelOptions,
  enrichModelSelectOptions,
  findSelectedModelOption,
  filterModelOptionsByProvider,
  filterModelProviderOptions,
  modelOptionBadges,
  modelSelectOptionMatchesSearch,
  modelOptionNeedsVisionRecommendation,
  modelOptionThinkingLevels,
  modelProviderOptions,
  modelSearchItemToModelSelectOption,
  mergeRegisteredModelProfileOptions,
  parseModelProviderQuery,
  parseModelAllowlist,
  serializeModelAllowlist,
  type ModelSelectOption,
} from "./modelSelect";
import {
  SettingsModelSearchField,
  modelSelectOptionsForSettingsTarget,
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

  assert.match(html, /使用するAPI/);
  assert.match(html, /aria-label="使用するAPI"/);
  assert.match(html, /<select/);
  assert.match(html, /登録済みの接続がありません/);
  assert.doesNotMatch(html, /provider\.deepseek\.main/);
  assert.doesNotMatch(html, /モデル設定ID/);
});

test("catalog picker searches the complete list and retains the selected model", () => {
  const models = Array.from({ length: 80 }, (_, index) => ({
    model_id: `vendor/model-${index}`, display_name: `Model ${index}`, type: "chat",
  }));
  const html = renderToStaticMarkup(createElement(CatalogModelPicker, {
    models, value: "vendor/model-70", query: "Model 79",
    onChange: () => {}, onQueryChange: () => {},
  }));
  assert.equal((html.match(/<input\b/g) ?? []).length, 1);
  assert.match(html, /role="combobox"/);
  assert.doesNotMatch(html, /<select/);
  assert.match(html, /vendor\/model-79/);
  assert.match(html, /選択中:.*vendor\/model-70/);
  assert.doesNotMatch(html, /Model 1<\/span>/);
});

test("catalog model queries disable linguistic input rewriting", () => {
  const html = renderToStaticMarkup(createElement(CatalogModelPicker, {
    models: [], value: "", query: "", onChange: () => {}, onQueryChange: () => {},
  }));
  const inputs = html.match(/<input\b[^>]*>/g) ?? [];
  assert.equal(inputs.length, 1);
  for (const input of inputs) {
    assert.match(input, /autoCorrect="off"/);
    assert.match(input, /autoCapitalize="none"/);
    assert.match(input, /spellCheck="false"/);
  }
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

test("model route save errors preserve a redacted native cause", () => {
  const message = modelRouteSaveErrorMessage(new Error(
    "request http://127.0.0.1:1234/v1 failed: Authorization: Bearer local-secret-value api_key=another-secret",
  ));

  assert.match(message, /モデルルートを保存できませんでした/);
  assert.doesNotMatch(message, /local-secret-value|another-secret|127\.0\.0\.1/);
  assert.match(message, /\[url\]|\[auth-header\]|\[credential\]/);
});

test("provider connection errors preserve a redacted native cause", () => {
  const message = modelRouteConnectionsErrorMessage(new Error(
    "request http://127.0.0.1:1234/v1 failed: Authorization: Bearer local-secret-value api_key=another-secret",
  ));

  assert.match(message, /Provider接続一覧を確認できません/);
  assert.doesNotMatch(message, /local-secret-value|another-secret|127\.0\.0\.1/);
  assert.match(message, /\[url\]|\[auth-header\]|\[credential\]/);
});

test("provider readiness identifies credentialless local connections", () => {
  const html = renderToStaticMarkup(createElement(ProviderReadiness, {
    connection: {
      provider_instance_id: "provider.openai_compatible.gemma3-1b",
      display_name: "Local Gemma 3 1B",
      credential_status: "not_required",
      health_status: "unverified",
      reachability: "unknown",
      observed_at: null,
    },
  }));

  assert.match(html, /資格情報: 不要（ローカル接続）/);
  assert.match(html, /到達性: 未確認/);
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

test("settings main and lightweight slots include saved routes with canonical identities", () => {
  const catalogOptions: ModelSelectOption[] = [
    {
      value: "provider.openai_compatible.gemma1b/gemma-3-1b-it",
      label: "Catalog Gemma",
      provider_id: "provider.openai_compatible.gemma1b",
      model_id: "gemma-3-1b-it",
    },
    { value: "stub/default", label: "Stub Default", provider_id: "stub", model_id: "default" },
  ];
  const route = {
    profile_id: "gemma1b-local",
    display_name: "Gemma 3 1B Local",
    provider_id: "provider.openai_compatible.gemma1b",
    provider_display_name: "Local Gemma",
    model_id: "gemma-3-1b-it",
    route_configured: true,
    local: true,
    supports_thinking: true,
    thinking_levels: ["low", "high"],
  };

  const mainOptions = modelSelectOptionsForSettingsTarget("main_model", catalogOptions, [route]);
  const lightweightOptions = modelSelectOptionsForSettingsTarget("lightweight_model", catalogOptions, [route]);
  for (const options of [mainOptions, lightweightOptions]) {
    assert.deepEqual(options.map((option) => option.value), ["stub/default", "gemma1b-local"]);
    const saved = options[1];
    assert.equal(saved.label, "Gemma 3 1B Local");
    assert.equal(saved.provider_id, "provider.openai_compatible.gemma1b");
    assert.equal(saved.provider_display_name, "Local Gemma");
    assert.equal(saved.model_id, "gemma-3-1b-it");
    assert.equal(saved.configured, true);
    assert.equal(saved.local, true);
    assert.deepEqual(saved.thinking_levels, ["low", "high"]);
  }
});

test("saved routes replace matching catalog aliases without hiding unrelated options", () => {
  const options = mergeRegisteredModelProfileOptions([
    { value: "gemma1b-local", label: "Duplicate saved route" },
    {
      value: "provider.openai_compatible.gemma1b/gemma-3-1b-it",
      label: "Duplicate provider/model alias",
      provider_id: "provider.openai_compatible.gemma1b",
      model_id: "gemma-3-1b-it",
    },
    { value: "other/model", label: "Other Model" },
  ], [{
    profile_id: "gemma1b-local",
    display_name: "Gemma 3 1B Local",
    provider_id: "provider.openai_compatible.gemma1b",
    model_id: "gemma-3-1b-it",
    route_configured: true,
  }, {
    profile_id: "not-saved",
    display_name: "Not Saved",
    provider_id: "provider.other",
    model_id: "other",
    route_configured: false,
  }]);

  assert.deepEqual(options.map((option) => option.value), ["other/model", "gemma1b-local"]);
  assert.equal(options[1].label, "Gemma 3 1B Local");
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


test("provider filters preserve saved route identity and distinguish transport from maker", () => {
  const saved: ModelSelectOption = { value: "saved-route", label: "Gemini via my API", provider_id: "provider.openrouter.main", model_id: "google/gemini-2.5-pro" };
  assert.deepEqual(filterModelOptionsByProvider([saved], "openrouter"), [saved]);
  assert.deepEqual(filterModelOptionsByProvider([saved], "google"), []);
  assert.equal(saved.value, "saved-route");
  const opaque = { ...saved, provider_id: "private-api-01", catalog_provider_id: "openrouter" };
  assert.deepEqual(filterModelOptionsByProvider([opaque], "openrouter"), [opaque]);
});

test("the shared model search retains OpenRouter HY3 legacy aliases", () => {
  const option = { value: "hy3-saved", label: "Tencent", provider_id: "provider.openrouter.main", model_id: "tencent/hy3" };
  assert.equal(modelSelectOptionMatchesSearch(option, "hy3 free current"), true);
  assert.equal(modelSelectOptionMatchesSearch({ ...option, model_id: "tencent/hy3-preview" }, "hy3 preview free"), true);
  assert.equal(modelSelectOptionMatchesSearch({ ...option, provider_id: "provider.google.main" }, "hy3 free"), false);
});


test("shared caller preserves the explicit result limit and catalogue ordering policy", () => {
  const options = ["Alpha", "Beta", "Gamma"].map((label) => ({ value: label, label, provider_id: "openai" }));
  const render = (position: "first" | "natural") => renderToStaticMarkup(createElement(ModelSearchPicker, {
    value: "Gamma", options, query: "", open: true, showTrigger: false, maxVisibleOptions: 2,
    selectorSchema: { ...DEFAULT_MODEL_SELECTOR_SCHEMA, layout: { ...DEFAULT_MODEL_SELECTOR_SCHEMA.layout, selected_position: position } },
    onChange: () => {}, onQueryChange: () => {},
  }));
  const selectedFirst = render("first");
  assert.equal((selectedFirst.match(/role="option"/g) ?? []).length, 2);
  assert.ok(selectedFirst.indexOf(">Gamma</span>") < selectedFirst.indexOf(">Alpha</span>"));
  const catalogOrder = render("natural");
  assert.match(catalogOrder, />Alpha<\/span>/);
  assert.match(catalogOrder, />Beta<\/span>/);
  assert.doesNotMatch(catalogOrder, />Gamma<\/span>/);
});
