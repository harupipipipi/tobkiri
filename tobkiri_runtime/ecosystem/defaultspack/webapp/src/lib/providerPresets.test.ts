import assert from "node:assert/strict";
import test from "node:test";
import { BUILTIN_API_PROVIDER_IDS } from "../features/apiKeys/apiKeySetup";

import {
  CUSTOM_PROVIDER_ID,
  isCustomProviderSetup,
  providerSetupLabel,
  providerSetupPreset,
  supportsSimpleProviderSetup,
} from "./providerPresets";

test("known hosted providers resolve their catalog endpoint and adapter", () => {
  assert.deepEqual(providerSetupPreset("openai"), {
    endpoint: "https://api.openai.com/v1",
    protocol: "openai-compatible",
  });
  assert.deepEqual(providerSetupPreset("google"), {
    endpoint: "https://generativelanguage.googleapis.com/v1beta/openai",
    protocol: "openai-compatible",
  });
  assert.deepEqual(providerSetupPreset("anthropic"), {
    endpoint: "https://api.anthropic.com",
    protocol: "anthropic",
  });
});

test("only Custom and unregistered provider ids require endpoint details", () => {
  assert.equal(isCustomProviderSetup("openai"), false);
  assert.equal(isCustomProviderSetup(CUSTOM_PROVIDER_ID), true);
  assert.equal(isCustomProviderSetup("acme-private"), true);
  assert.equal(supportsSimpleProviderSetup("openai"), true);
  assert.equal(supportsSimpleProviderSetup(CUSTOM_PROVIDER_ID), true);
  assert.equal(supportsSimpleProviderSetup("ollama"), false);
  assert.equal(providerSetupLabel(CUSTOM_PROVIDER_ID, "OpenAI Compatible"), "Custom");
});

test("all bundled hosted key providers have an HTTPS preset", () => {
  const localOrCustom = new Set(["openai_compatible", "ollama", "llama_cpp", "lmstudio", "vllm"]);
  for (const id of BUILTIN_API_PROVIDER_IDS.filter((id) => !localOrCustom.has(id))) {
    const preset = providerSetupPreset(id);
    assert.ok(preset, `${id} needs its declared hosted connection`);
    assert.equal(new URL(preset.endpoint).protocol, "https:");
  }
});
