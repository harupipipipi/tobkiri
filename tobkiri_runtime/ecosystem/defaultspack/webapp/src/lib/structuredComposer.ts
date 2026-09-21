import type { TemplateComposerField } from "./api";

export type StructuredComposerValues = Record<string, string>;

function cleanText(value: unknown, maxLength = 120): string {
  return typeof value === "string"
    ? value.replace(/[\u0000-\u001f\u007f]/g, " ").replace(/\s+/g, " ").trim().slice(0, maxLength)
    : "";
}

export function normalizeComposerFields(value: unknown): TemplateComposerField[] {
  if (!Array.isArray(value)) return [];
  const seen = new Set<string>();
  const fields: TemplateComposerField[] = [];
  for (const candidate of value.slice(0, 16)) {
    if (!candidate || typeof candidate !== "object" || Array.isArray(candidate)) continue;
    const raw = candidate as Record<string, unknown>;
    const id = cleanText(raw.id, 64).replace(/[^a-zA-Z0-9_.-]/g, "_");
    if (!id || seen.has(id)) continue;
    const type = raw.type === "text" || raw.type === "textarea" ? raw.type : "select";
    const options = Array.isArray(raw.options)
      ? raw.options.slice(0, 40).map((option) => {
          if (typeof option === "string") return { value: cleanText(option), label: cleanText(option) };
          if (!option || typeof option !== "object") return null;
          const item = option as Record<string, unknown>;
          const optionValue = cleanText(item.value, 120);
          if (!optionValue) return null;
          return { value: optionValue, label: cleanText(item.label, 120) || optionValue };
        }).filter((option): option is { value: string; label: string } => Boolean(option))
      : [];
    if (type === "select" && options.length === 0) continue;
    seen.add(id);
    fields.push({
      id,
      type,
      label: cleanText(raw.label, 80) || id,
      description: cleanText(raw.description, 180),
      placeholder: cleanText(raw.placeholder, 120),
      default: cleanText(raw.default, 240),
      required: raw.required === true,
      options,
    });
  }
  return fields;
}

export function initialComposerFieldValues(fields: TemplateComposerField[]): StructuredComposerValues {
  return Object.fromEntries(fields.map((field) => [field.id, field.default || field.options?.[0]?.value || ""]));
}

export function structuredComposerPayload(
  fields: TemplateComposerField[],
  values: StructuredComposerValues,
): Record<string, string> {
  return Object.fromEntries(fields
    .map((field) => [field.id, cleanText(values[field.id], field.type === "textarea" ? 1200 : 240)] as const)
    .filter(([, value]) => Boolean(value)));
}
