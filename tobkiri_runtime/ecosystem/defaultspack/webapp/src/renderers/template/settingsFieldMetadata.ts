import type { SettingsSection, UICatalog } from "../../lib/api";

export type TemplateSettingsFieldType =
  | SettingsSection["fields"][number]["type"]
  | "model_select"
  | "provider_select"
  | "api_key_setup"
  | (string & {});

export type TemplateSettingsFieldRendererRef = string | {
  id?: string;
  component?: string;
  kind?: string;
  props?: Record<string, unknown>;
};

export type TemplateSettingsFieldMetadata = {
  type: TemplateSettingsFieldType;
  renderer?: TemplateSettingsFieldRendererRef;
  component?: string;
  part_id?: string;
  binding?: string;
  catalog_binding?: string;
  renderer_props?: Record<string, unknown>;
  selector_schema?: Record<string, unknown>;
};

export type TemplateSettingsField = Omit<SettingsSection["fields"][number], "type" | "renderer"> & TemplateSettingsFieldMetadata;

export type TemplateComponentBinding = NonNullable<UICatalog["component_bindings"]>[number];

function rendererRefKeys(ref: TemplateSettingsFieldRendererRef | undefined): string[] {
  if (!ref) return [];
  if (typeof ref === "string") return [ref];
  return [ref.id, ref.component, ref.kind].filter((item): item is string => Boolean(item));
}

export function settingsFieldRendererLookupKeys(
  field: TemplateSettingsField,
  componentBindings: TemplateComponentBinding[] = [],
): string[] {
  const explicitKeys = [
    ...rendererRefKeys(field.renderer),
    field.component,
    field.binding,
    field.catalog_binding,
    field.part_id,
  ].filter((item): item is string => Boolean(item));
  const bindingKeys = componentBindings
    .filter((binding) => explicitKeys.includes(binding.part_id) || binding.part_id === field.id)
    .flatMap((binding) => [binding.component, binding.part_id]);
  const keys = [...explicitKeys, ...bindingKeys, String(field.type)];
  return [...new Set(keys.map((key) => key.trim()).filter(Boolean))];
}
