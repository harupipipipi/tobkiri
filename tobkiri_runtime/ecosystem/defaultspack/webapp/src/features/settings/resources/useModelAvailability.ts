import type { ModelProfile } from "../../../lib/api";

export type ModelCandidate = {
  provider_id: string;
  model_id: string;
  label?: string;
  profile_id?: string;
};

export type ModelAvailabilityAfterKeySave =
  | {
      status: "saved";
    }
  | {
      status: "models_available";
      profiles: ModelProfile[];
      selected_profile_id: string;
    }
  | {
      status: "route_required";
      provider_id: string;
      api_id: string;
      candidate_models: ModelCandidate[];
      reason: string;
    };

export function isModelsAvailable(
  value: ModelAvailabilityAfterKeySave | null | undefined,
): value is Extract<ModelAvailabilityAfterKeySave, { status: "models_available" }> {
  return value?.status === "models_available" && value.profiles.length > 0 && Boolean(value.selected_profile_id);
}

export function availabilityCopy(value: ModelAvailabilityAfterKeySave | null | undefined): {
  tone: "success" | "warning" | "idle";
  text: string;
} {
  if (!value) return { tone: "idle", text: "" };
  if (value.status === "saved") {
    return {
      tone: "success",
      text: "API key saved.",
    };
  }
  if (isModelsAvailable(value)) {
    const selected = value.profiles.find((profile) => profile.profile_id === value.selected_profile_id);
    return {
      tone: "success",
      text: `Saved. Model is available${selected?.display_name ? `: ${selected.display_name}` : "."}`,
    };
  }
  return {
    tone: "warning",
    text: value.reason || "Saved, but select a model route before this key can appear in the composer.",
  };
}
