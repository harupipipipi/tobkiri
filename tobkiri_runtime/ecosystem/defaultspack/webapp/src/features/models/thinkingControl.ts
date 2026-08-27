import type { ModelProfile, ThinkingControlContract } from "../../lib/api";

export type ThinkingControlMode = "none" | "number" | "enum" | "text";

export function thinkingControlForProfile(profile?: ModelProfile | null): ThinkingControlContract {
  if (profile?.thinking_control?.source !== "legacy" && profile?.thinking_control?.input_schema) {
    return profile.thinking_control;
  }
  const values = profile?.thinking_levels?.length
    ? profile.thinking_levels
    : ["low", "medium", "high"];
  return {
    supported: Boolean(profile?.supports_thinking),
    input_schema: { type: "enum", values },
    request_binding: {},
    source: "legacy",
  };
}

export function thinkingControlMode(profile?: ModelProfile | null): ThinkingControlMode {
  const contract = thinkingControlForProfile(profile);
  if (!contract.supported) return "none";
  const type = contract.input_schema?.type;
  return type === "number" || type === "enum" || type === "text" ? type : "none";
}

export function thinkingControlCandidates(profile?: ModelProfile | null): string[] {
  const contract = thinkingControlForProfile(profile);
  if (contract.input_schema?.type !== "enum") return [];
  return (contract.input_schema.values ?? []).map(String).filter(Boolean);
}

export function thinkingControlInputError(
  profile: ModelProfile | null | undefined,
  value: string,
): string | null {
  const contract = thinkingControlForProfile(profile);
  const schema = contract.input_schema;
  const raw = value.trim();
  if (schema?.allow_auto && raw === "auto") return null;
  if (schema?.type === "enum") {
    return thinkingControlCandidates(profile).includes(raw)
      ? null
      : "Select a value declared by this model profile.";
  }
  if (schema?.type === "number") {
    if (!/^(?:\d+(?:\.\d*)?|\.\d+)[kKmMbB]?$/.test(raw)) {
      return "Enter a number with an optional k, m, or b suffix.";
    }
    const suffix = raw.slice(-1).toLowerCase();
    const multiplier = suffix === "k" ? 1_000 : suffix === "m" ? 1_000_000 : suffix === "b" ? 1_000_000_000 : 1;
    const numeric = Number(multiplier === 1 ? raw : raw.slice(0, -1)) * multiplier;
    if (!Number.isFinite(numeric)) return "Enter a finite number.";
    if (typeof schema.min === "number" && numeric < schema.min) return `Minimum: ${schema.min}`;
    if (typeof schema.max === "number" && numeric > schema.max) return `Maximum: ${schema.max}`;
    if (typeof schema.step === "number" && schema.step > 0) {
      const origin = typeof schema.min === "number" ? schema.min : 0;
      if (Math.abs((numeric - origin) % schema.step) > Number.EPSILON) return `Step: ${schema.step}`;
    }
    return null;
  }
  if (schema?.type === "text") {
    const maxLength = schema.max_length ?? 64;
    if (!raw || raw.length > maxLength) return `Enter 1–${maxLength} characters.`;
    if (schema.pattern) {
      try {
        if (!new RegExp(`^(?:${schema.pattern})$`).test(raw)) return "Value does not match this model profile.";
      } catch {
        return "This model profile declares an invalid pattern.";
      }
    }
    return null;
  }
  return "Thinking control is not supported by this model profile.";
}

export function normalizeThinkingControlInput(
  profile: ModelProfile | null | undefined,
  value: string,
): string | number {
  const schema = thinkingControlForProfile(profile).input_schema;
  const raw = value.trim();
  if (schema?.allow_auto && raw === "auto" && schema.auto_value !== undefined) {
    return schema.auto_value;
  }
  if (schema?.type !== "number") return raw;
  const suffix = raw.slice(-1).toLowerCase();
  const multiplier = suffix === "k" ? 1_000 : suffix === "m" ? 1_000_000 : suffix === "b" ? 1_000_000_000 : 1;
  return Number(multiplier === 1 ? raw : raw.slice(0, -1)) * multiplier;
}
