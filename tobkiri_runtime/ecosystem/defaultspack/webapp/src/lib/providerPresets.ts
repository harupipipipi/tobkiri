/**
 * Known hosted provider connection details.
 *
 * This deliberately mirrors the public endpoints declared by the canonical
 * provider catalog. Keeping the small UI-facing projection here means the
 * setup form can collect only a provider, a connection name, and a key. A
 * custom endpoint still has to be chosen explicitly and is never guessed.
 */
export type ProviderSetupProtocol = "openai-compatible" | "anthropic";

export type ProviderSetupPreset = Readonly<{
  endpoint: string;
  protocol: ProviderSetupProtocol;
}>;

export const CUSTOM_PROVIDER_ID = "openai_compatible";

const OPENAI_COMPATIBLE = "openai-compatible" as const;

const PROVIDER_SETUP_PRESETS: Readonly<Record<string, ProviderSetupPreset>> = {
  anthropic: { endpoint: "https://api.anthropic.com", protocol: "anthropic" },
  openai: { endpoint: "https://api.openai.com/v1", protocol: OPENAI_COMPATIBLE },
  google: {
    endpoint: "https://generativelanguage.googleapis.com/v1beta/openai",
    protocol: OPENAI_COMPATIBLE,
  },
  "gitlawb-opengateway": {
    endpoint: "https://opengateway.gitlawb.com/v1",
    protocol: OPENAI_COMPATIBLE,
  },
  openrouter: { endpoint: "https://openrouter.ai/api/v1", protocol: OPENAI_COMPATIBLE },
  "opencode-go": { endpoint: "https://opencode.ai/zen/go/v1", protocol: OPENAI_COMPATIBLE },
  "opencode-zen": { endpoint: "https://opencode.ai/zen/v1", protocol: OPENAI_COMPATIBLE },
  "xiaomi-token-plan-ams": { endpoint: "https://token-plan-ams.xiaomimimo.com/v1", protocol: OPENAI_COMPATIBLE },
  "xiaomi-token-plan-cn": { endpoint: "https://token-plan-cn.xiaomimimo.com/v1", protocol: OPENAI_COMPATIBLE },
  "xiaomi-token-plan-sgp": { endpoint: "https://token-plan-sgp.xiaomimimo.com/v1", protocol: OPENAI_COMPATIBLE },
  avian: { endpoint: "https://api.avian.io/v1", protocol: OPENAI_COMPATIBLE },
  cerebras: { endpoint: "https://api.cerebras.ai/v1", protocol: OPENAI_COMPATIBLE },
  deepseek: { endpoint: "https://api.deepseek.com/v1", protocol: OPENAI_COMPATIBLE },
  deepinfra: { endpoint: "https://api.deepinfra.com/v1/openai", protocol: OPENAI_COMPATIBLE },
  fireworks: { endpoint: "https://api.fireworks.ai/inference/v1", protocol: OPENAI_COMPATIBLE },
  friendli: { endpoint: "https://api.friendli.ai/serverless/v1", protocol: OPENAI_COMPATIBLE },
  glm: { endpoint: "https://api.z.ai/api/paas/v4", protocol: OPENAI_COMPATIBLE },
  groq: { endpoint: "https://api.groq.com/openai/v1", protocol: OPENAI_COMPATIBLE },
  hyperbolic: { endpoint: "https://api.hyperbolic.xyz/v1", protocol: OPENAI_COMPATIBLE },
  "inference-net": { endpoint: "https://api.inference.net/v1", protocol: OPENAI_COMPATIBLE },
  longcat: { endpoint: "https://api.longcat.chat/openai/v1", protocol: OPENAI_COMPATIBLE },
  mistral: { endpoint: "https://api.mistral.ai/v1", protocol: OPENAI_COMPATIBLE },
  moonshotai: { endpoint: "https://api.moonshot.ai/v1", protocol: OPENAI_COMPATIBLE },
  nebius: { endpoint: "https://api.studio.nebius.ai/v1", protocol: OPENAI_COMPATIBLE },
  novita: { endpoint: "https://api.novita.ai/openai/v1", protocol: OPENAI_COMPATIBLE },
  nvidia: { endpoint: "https://integrate.api.nvidia.com/v1", protocol: OPENAI_COMPATIBLE },
  perplexity: { endpoint: "https://api.perplexity.ai/v1", protocol: OPENAI_COMPATIBLE },
  sambanova: { endpoint: "https://api.sambanova.ai/v1", protocol: OPENAI_COMPATIBLE },
  together: { endpoint: "https://api.together.xyz/v1", protocol: OPENAI_COMPATIBLE },
  upstage: { endpoint: "https://api.upstage.ai/v1", protocol: OPENAI_COMPATIBLE },
  xai: { endpoint: "https://api.x.ai/v1", protocol: OPENAI_COMPATIBLE },
};

function normalizedProviderId(providerId: string): string {
  return providerId.trim().toLowerCase();
}

/** Return the catalog endpoint and protocol that can be used without a form override. */
export function providerSetupPreset(providerId: string): ProviderSetupPreset | null {
  return PROVIDER_SETUP_PRESETS[normalizedProviderId(providerId)] ?? null;
}

/** A custom connection is the only setup path that asks for endpoint details. */
export function isCustomProviderSetup(providerId: string): boolean {
  const normalized = normalizedProviderId(providerId);
  return normalized === CUSTOM_PROVIDER_ID || !providerSetupPreset(normalized);
}

/** Keep the generic endpoint recognizable in compact provider controls. */
export function providerSetupLabel(providerId: string, fallback: string): string {
  return normalizedProviderId(providerId) === CUSTOM_PROVIDER_ID ? "Custom" : fallback;
}

/** Built-ins without a secure hosted preset belong behind the explicit Custom flow. */
export function supportsSimpleProviderSetup(providerId: string): boolean {
  const normalized = normalizedProviderId(providerId);
  return normalized === CUSTOM_PROVIDER_ID || providerSetupPreset(normalized) !== null;
}
