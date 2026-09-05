import test from "node:test";
import assert from "node:assert/strict";

import {
  buildApiKeySavePayload,
  collectApiProviderOptions,
  collectExternalProviderOptions,
  customProviderRegistrationPayload,
  filterApiProviderOptions,
  filterApiProviderOptionsByScope,
  filterRegisteredApiRowsByScope,
  normalizeApiProviderScope,
  normalizeCustomProviderId,
  parseAllowedModels,
  summarizeApiKeySetupForDiagnostics,
} from "./apiKeySetup";

test("collectApiProviderOptions includes builtins, custom providers, and OAuth metadata", () => {
  const options = collectApiProviderOptions([
    {
      provider_id: "acme-ai",
      label: "Acme AI",
      kind: "llm",
      oauth: { connected: true, client_configured: true },
    },
    {
      provider_id: "searchapi",
      label: "Search API",
      kind: "custom",
      builtin: false,
    },
  ]);

  const google = options.find((option) => option.provider_id === "google");
  const acme = options.find((option) => option.provider_id === "acme-ai");
  const searchapi = options.find((option) => option.provider_id === "searchapi");

  assert.equal(google?.builtin, true);
  assert.equal(acme?.kind, "llm");
  assert.equal(acme?.oauth_supported, true);
  assert.equal(acme?.oauth_connected, true);
  assert.equal(acme?.oauth_client_configured, true);
  assert.equal(searchapi?.kind, "custom");
  assert.equal(searchapi?.builtin, false);
  assert.equal(options.find((option) => option.provider_id === "cloudflare")?.kind, "custom");
});

test("collectExternalProviderOptions keeps external providers custom", () => {
  const options = collectExternalProviderOptions([
    { provider_id: "line", label: "LINE", kind: "llm" },
    { provider_id: "internal-webhook", label: "Internal Webhook" },
  ]);

  assert.equal(options.find((option) => option.provider_id === "line")?.kind, "custom");
  assert.equal(options.find((option) => option.provider_id === "internal-webhook")?.kind, "custom");
});

test("filterApiProviderOptions searches label and provider id", () => {
  const options = collectApiProviderOptions([{ provider_id: "acme-ai", label: "Acme AI" }]);

  assert.deepEqual(filterApiProviderOptions(options, "acme").map((option) => option.provider_id), ["acme-ai"]);
  assert(filterApiProviderOptions(options, "OpenAI").some((option) => option.provider_id === "openai"));
});

test("API provider scope keeps AI and non-AI credential surfaces separate", () => {
  const options = collectApiProviderOptions([
    { provider_id: "openai", label: "OpenAI", kind: "llm" },
    { provider_id: "cloudflare", label: "Cloudflare", kind: "custom" },
  ]);

  assert.equal(filterApiProviderOptionsByScope(options, "llm").some((option) => option.provider_id === "openai"), true);
  assert.equal(filterApiProviderOptionsByScope(options, "non_llm").some((option) => option.provider_id === "openai"), false);
  assert.equal(filterApiProviderOptionsByScope(options, "non_llm").some((option) => option.provider_id === "cloudflare"), true);

  const rows = [
    { provider_id: "openai", api_id: "main", kind: "llm" },
    { provider_id: "cloudflare", api_id: "work", kind: "custom" },
  ];
  assert.deepEqual(
    filterRegisteredApiRowsByScope(rows, options, "non_llm").map((row) => row.provider_id),
    ["cloudflare"],
  );
});

test("normalizeApiProviderScope accepts declarative template aliases", () => {
  assert.equal(normalizeApiProviderScope("ai"), "llm");
  assert.equal(normalizeApiProviderScope("non-llm"), "non_llm");
  assert.equal(normalizeApiProviderScope("external"), "non_llm");
  assert.equal(normalizeApiProviderScope(undefined), "all");
});

test("custom provider registration normalizes provider ids", () => {
  assert.equal(normalizeCustomProviderId("  My Search/API  "), "my_search_api");
  assert.deepEqual(customProviderRegistrationPayload({
    providerId: "  My Search/API  ",
    label: "My Search",
    kind: "custom",
  }), {
    provider_id: "my_search_api",
    label: "My Search",
    kind: "custom",
  });
});

test("buildApiKeySavePayload parses form metadata while keeping secret only in save payload", () => {
  const payload = buildApiKeySavePayload({
    provider_id: "openai",
    name: "work",
    value: "sk-secret",
    kind: "llm",
    base_url: " https://example.test ",
    allowed_models: "gpt-4.1, gpt-4.1\n o4-mini ",
    default_model: "gpt-4.1",
    quota_label: "paid",
    notes: "private notes",
  });

  assert.equal(payload?.provider_id, "openai");
  assert.equal(payload?.value, "sk-secret");
  assert.deepEqual(payload?.options.allowedModels, ["gpt-4.1", "o4-mini"]);
  assert.equal(payload?.options.baseUrl, "https://example.test");
});

test("buildApiKeySavePayload accepts an explicit loopback no-key connection", () => {
  const payload = buildApiKeySavePayload({
    provider_id: "vllm",
    name: "local",
    value: "",
    base_url: "http://127.0.0.1:8000/v1",
    credential_mode: "none",
  });

  assert.equal(payload?.value, "");
  assert.equal(payload?.options.credentialMode, "none");
  assert.equal(payload?.options.baseUrl, "http://127.0.0.1:8000/v1");
});

test("summarizeApiKeySetupForDiagnostics never exposes secret values", () => {
  const summary = summarizeApiKeySetupForDiagnostics({
    provider_id: "openai",
    name: "main",
    value: "sk-secret-value",
    allowed_models: ["gpt-4.1"],
  });

  assert.equal(summary.has_secret, true);
  assert.equal(summary.secret_length, "sk-secret-value".length);
  assert.equal(JSON.stringify(summary).includes("sk-secret-value"), false);
});

test("parseAllowedModels dedupes comma and newline lists", () => {
  assert.deepEqual(parseAllowedModels("a,b\na"), ["a", "b"]);
});
