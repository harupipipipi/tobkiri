export type ModelStateWrite = {
  kind: "preferred_model" | "thinking_level" | "deepthink_enabled";
  value: unknown;
};

/** Model controls use the revisioned model-state API, not preferences-write. */
export function modelStateWriteForSettingsField(
  sectionId: string,
  fieldId: string,
  value: unknown,
): ModelStateWrite | null {
  if (sectionId !== "models") return null;
  if (fieldId === "main_model" || fieldId === "preferred_model") {
    return { kind: "preferred_model", value: String(value ?? "").trim() };
  }
  if (fieldId === "thinking_level" || fieldId === "deepthink_enabled") {
    return { kind: fieldId, value };
  }
  return null;
}
