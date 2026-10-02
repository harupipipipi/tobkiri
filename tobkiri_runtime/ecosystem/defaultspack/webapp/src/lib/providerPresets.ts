import providerModelCatalog from "./providerModelCatalog.generated.json";

/**
 * Known hosted provider connection details.
 *
 * These presets are generated from the canonical provider JSON catalog.
 * Keeping its small UI-facing projection here means the
 * setup form can collect only a provider, a connection name, and a key. A
 * custom endpoint still has to be chosen explicitly and is never guessed.
 */
export type ProviderSetupProtocol = "openai-compatible" | "anthropic";

export type ProviderSetupPreset = Readonly<{
  endpoint: string;
  protocol: ProviderSetupProtocol;
}>;

export const CUSTOM_PROVIDER_ID = "openai_compatible";

export const HOSTED_PROVIDER_IDS = Object.keys(providerModelCatalog.providers);

const PROVIDER_SETUP_PRESETS = providerModelCatalog.providers as Readonly<
  Record<string, ProviderSetupPreset & { display_name: string }>
>;

function normalizedProviderId(providerId: string): string {
  return providerId.trim().toLowerCase();
}

/** Return the catalog endpoint and protocol that can be used without a form override. */
export function providerSetupPreset(providerId: string): ProviderSetupPreset | null {
  const id = normalizedProviderId(providerId);
  if (!Object.prototype.hasOwnProperty.call(PROVIDER_SETUP_PRESETS, id)) return null;
  const preset = PROVIDER_SETUP_PRESETS[id];
  return preset ? { endpoint: preset.endpoint, protocol: preset.protocol } : null;
}

/** A custom connection is the only setup path that asks for endpoint details. */
export function isCustomProviderSetup(providerId: string): boolean {
  const normalized = normalizedProviderId(providerId);
  return normalized === CUSTOM_PROVIDER_ID || !providerSetupPreset(normalized);
}

/** Keep the generic endpoint recognizable in compact provider controls. */
export function providerSetupLabel(providerId: string, fallback: string): string {
  const normalized = normalizedProviderId(providerId);
  if (normalized === CUSTOM_PROVIDER_ID) return "Custom";
  return PROVIDER_SETUP_PRESETS[normalized]?.display_name ?? fallback;
}

/** Built-ins without a secure hosted preset belong behind the explicit Custom flow. */
export function supportsSimpleProviderSetup(providerId: string): boolean {
  const normalized = normalizedProviderId(providerId);
  return normalized === CUSTOM_PROVIDER_ID || providerSetupPreset(normalized) !== null;
}
