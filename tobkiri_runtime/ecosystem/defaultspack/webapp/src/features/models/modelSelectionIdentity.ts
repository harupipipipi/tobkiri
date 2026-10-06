import type { ModelProfile } from "../../lib/api";
import type { ModelSelectOption } from "./modelSelect";

/** Resolve a saved route's execution connection without inferring a manufacturer. */
export function modelProfileConnectionId(profile: ModelProfile): string {
  const metadata = profile.metadata;
  const explicit = metadata?.connection_id ?? metadata?.provider_instance_id;
  return typeof explicit === "string" && explicit.trim()
    ? explicit.trim()
    : String(profile.provider_id ?? "").trim();
}

/** Return only an exact saved route adapted locally; catalogue IDs cannot switch. */
export function savedModelProfileForOption(
  profiles: readonly ModelProfile[],
  option: ModelSelectOption | null,
): ModelProfile | null {
  if (!option?.registered_profile_id
    || option.registered_profile_id !== option.value) return null;
  const profile = profiles.find((item) => item.profile_id === option.value);
  if (!profile || !profile.provider_id || !profile.model_id) return null;
  if (option.provider_id !== profile.provider_id || option.model_id !== profile.model_id
    || option.connection_id !== modelProfileConnectionId(profile)) return null;
  return profile;
}
