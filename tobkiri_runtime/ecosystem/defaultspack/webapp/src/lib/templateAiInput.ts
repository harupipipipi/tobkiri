import type {
  ComposerCommandMode,
  ComposerWidgetAction,
  TemplateAiInput,
  TemplateCatalogMetadataItem,
  TemplateComposerInput,
  TemplateToolPolicy,
  UICatalog,
} from "./api";
import type { ComposerExtensionItem, DroppedWidget } from "../renderers/types";
import {
  mergeTemplateToolPolicies as mergeTemplateToolPoliciesCore,
  templateToolPolicySettings as templateToolPolicySettingsCore,
} from "./templateToolPolicyMerge";

export type { TemplateToolPolicySettings } from "./templateToolPolicyMerge";

function objectRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

function nonEmptyString(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

function stringList(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return [...new Set(value.map(nonEmptyString).filter(Boolean))];
}

function modesMatch(item: { modes?: ComposerCommandMode[] }, mode: ComposerCommandMode): boolean {
  return !item.modes?.length || item.modes.includes(mode);
}

function itemEnabled(item: { enabled?: boolean }): boolean {
  return item.enabled !== false;
}

function activeItems<T extends { enabled?: boolean; modes?: ComposerCommandMode[] }>(
  items: T[] | undefined,
  mode: ComposerCommandMode,
): T[] {
  return (items ?? []).filter((item) => itemEnabled(item) && modesMatch(item, mode));
}

function uniqueStrings(values: unknown[]): string[] {
  return [...new Set(values.map(nonEmptyString).filter(Boolean))];
}

function sourceIds(items: Array<{ id?: string }>): string[] {
  return uniqueStrings(items.map((item) => item.id));
}

function composedId(prefix: string, items: Array<{ id?: string }>): string {
  const ids = sourceIds(items);
  return ids.length <= 1 ? ids[0] ?? prefix : `${prefix}:${ids.join("+")}`;
}

function firstString<T>(items: T[], pick: (item: T) => unknown): string | undefined {
  for (const item of items) {
    const value = nonEmptyString(pick(item));
    if (value) return value;
  }
  return undefined;
}

function sameString<T>(items: T[], pick: (item: T) => unknown): string | undefined {
  const values = uniqueStrings(items.map(pick));
  return values.length === 1 ? values[0] : undefined;
}

function mergeStringLists<T>(items: T[], pick: (item: T) => unknown): string[] {
  return uniqueStrings(items.flatMap((item) => stringList(pick(item))));
}

function mergeRecordValues<T>(items: T[], pick: (item: T) => unknown): Record<string, unknown> | undefined {
  const merged: Record<string, unknown> = {};
  for (const item of items) {
    const record = objectRecord(pick(item));
    if (record) Object.assign(merged, record);
  }
  return Object.keys(merged).length ? merged : undefined;
}

function mergeModes(items: Array<{ modes?: ComposerCommandMode[] }>): ComposerCommandMode[] | undefined {
  const modes = uniqueStrings(items.flatMap((item) => item.modes ?? [])) as ComposerCommandMode[];
  return modes.length ? modes : undefined;
}

function mergeFeatureFlags(items: Array<{ feature_flags?: Record<string, boolean | string | number | null | undefined> }>): Record<string, boolean | string | number | null | undefined> | undefined {
  const merged: Record<string, boolean | string | number | null | undefined> = {};
  for (const item of items) {
    const flags = objectRecord(item.feature_flags);
    if (!flags) continue;
    for (const [key, value] of Object.entries(flags)) {
      if (!key || value === undefined) continue;
      if (!(key in merged)) {
        merged[key] = value as boolean | string | number | null;
        continue;
      }
      const current = merged[key];
      if (typeof current === "boolean" && typeof value === "boolean") {
        merged[key] = current && value;
      } else if (current !== value) {
        merged[key] = value as boolean | string | number | null;
      }
    }
  }
  return Object.keys(merged).length ? merged : undefined;
}

function sourceMetadata(items: Array<{ id?: string; metadata?: Record<string, unknown> }>): Record<string, unknown> {
  const ids = sourceIds(items);
  return {
    ...(items[0]?.metadata ?? {}),
    ...(ids.length ? { source_ids: ids } : {}),
  };
}

function aiInputComposerIds(input: TemplateAiInput): string[] {
  return uniqueStrings([input.composer_input_id, input.composer_input]);
}

function aiInputToolPolicyIds(input: TemplateAiInput): string[] {
  return uniqueStrings([input.tool_policy_id, input.tool_policy]);
}

function sourceIdsFromMetadata(value: unknown): string[] {
  return stringList(objectRecord(value)?.source_ids);
}

function selectedAiInputs(
  catalog: UICatalog | null | undefined,
  mode: ComposerCommandMode,
  aiInput: TemplateAiInput | TemplateAiInput[] | null,
): TemplateAiInput[] {
  if (Array.isArray(aiInput)) return aiInput.filter((item) => itemEnabled(item) && modesMatch(item, mode));
  if (!aiInput) return [];
  const selectedSourceIds = new Set(sourceIdsFromMetadata(aiInput.metadata));
  if (selectedSourceIds.size > 0) {
    return activeItems(catalog?.ai_inputs, mode).filter((item) => selectedSourceIds.has(item.id));
  }
  return itemEnabled(aiInput) && modesMatch(aiInput, mode) ? [aiInput] : [];
}

function mergeTemplateAiInputs(items: TemplateAiInput[]): TemplateAiInput | null {
  if (items.length === 0) return null;
  if (items.length === 1) return items[0];
  const metadata = sourceMetadata(items);
  const composerInputIds = uniqueStrings(items.flatMap(aiInputComposerIds));
  const toolPolicyIds = uniqueStrings(items.flatMap(aiInputToolPolicyIds));
  return {
    id: composedId("composed_ai_input", items),
    label: firstString(items, (item) => item.label),
    description: firstString(items, (item) => item.description),
    widgets: mergeStringLists(items, (item) => item.widgets),
    params: mergeRecordValues(items, (item) => item.params),
    modes: mergeModes(items),
    enabled: true,
    template_id: sameString(items, (item) => item.template_id),
    piece_id: sameString(items, (item) => item.piece_id),
    origin: items[0]?.origin,
    metadata: {
      ...metadata,
      ...(composerInputIds.length ? { composer_input_ids: composerInputIds } : {}),
      ...(toolPolicyIds.length ? { tool_policy_ids: toolPolicyIds } : {}),
    },
  };
}

function mergeTemplateComposerInputs(items: TemplateComposerInput[]): TemplateComposerInput | null {
  if (items.length === 0) return null;
  if (items.length === 1) return items[0];
  const widgetIds = mergeStringLists(items, (item) => objectRecord(item)?.widgets);
  return {
    id: composedId("composed_composer_input", items),
    label: firstString(items, (item) => item.label),
    description: firstString(items, (item) => item.description),
    placeholder: firstString(items, (item) => item.placeholder),
    help: firstString(items, (item) => item.help),
    layout: mergeRecordValues(items, (item) => item.layout) as TemplateComposerInput["layout"],
    accepted_modalities: mergeStringLists(items, (item) => item.accepted_modalities),
    feature_flags: mergeFeatureFlags(items),
    fields: items.flatMap((item) => Array.isArray(item.fields) ? item.fields : []),
    field_layout: firstString(items, (item) => item.field_layout) as TemplateComposerInput["field_layout"],
    modes: mergeModes(items),
    enabled: true,
    component: firstString(items, (item) => item.component),
    renderer: firstString(items, (item) => item.renderer),
    template_id: sameString(items, (item) => item.template_id),
    piece_id: sameString(items, (item) => item.piece_id),
    origin: items[0]?.origin,
    metadata: sourceMetadata(items),
    ...(widgetIds.length ? { widgets: widgetIds } : {}),
  } as TemplateComposerInput;
}

export function selectTemplateAiInput(catalog: UICatalog | null | undefined, mode: ComposerCommandMode): TemplateAiInput | null {
  return mergeTemplateAiInputs(activeItems(catalog?.ai_inputs, mode));
}

export function selectTemplateComposerInput(
  catalog: UICatalog | null | undefined,
  mode: ComposerCommandMode,
  aiInput: TemplateAiInput | TemplateAiInput[] | null,
): TemplateComposerInput | null {
  const inputs = catalog?.composer_inputs ?? [];
  const activeInputs = activeItems(inputs, mode);
  const aiInputs = selectedAiInputs(catalog, mode, aiInput);
  const requestedIds = uniqueStrings([
    ...aiInputs.flatMap(aiInputComposerIds),
    ...stringList(objectRecord(Array.isArray(aiInput) ? null : aiInput?.metadata)?.composer_input_ids),
  ]);
  if (requestedIds.length > 0) {
    const requestedIdSet = new Set(requestedIds);
    const requested = activeInputs.filter((item) => requestedIdSet.has(item.id));
    if (requested.length) return mergeTemplateComposerInputs(requested);
  }
  return mergeTemplateComposerInputs(activeInputs);
}

export function selectTemplateToolPolicy(
  catalog: UICatalog | null | undefined,
  mode: ComposerCommandMode,
  aiInput: TemplateAiInput | TemplateAiInput[] | null,
): TemplateToolPolicy | null {
  const policies = catalog?.tool_policies ?? [];
  const activePolicies = activeItems(policies, mode);
  const aiInputs = selectedAiInputs(catalog, mode, aiInput);
  const requestedIds = uniqueStrings([
    ...aiInputs.flatMap(aiInputToolPolicyIds),
    ...stringList(objectRecord(Array.isArray(aiInput) ? null : aiInput?.metadata)?.tool_policy_ids),
  ]);
  if (requestedIds.length > 0) {
    const requestedIdSet = new Set(requestedIds);
    const requested = activePolicies.filter((item) => requestedIdSet.has(item.id));
    if (requested.length) return mergeTemplateToolPoliciesCore(requested);
  }
  return mergeTemplateToolPoliciesCore(activePolicies);
}

export function templateAiInputSourceIds(input: TemplateAiInput | null | undefined): string[] {
  if (!input) return [];
  const metadataIds = sourceIdsFromMetadata(input.metadata);
  return metadataIds.length ? metadataIds : input.id ? [input.id] : [];
}

export function templateComposerInputSourceIds(input: TemplateComposerInput | null | undefined): string[] {
  if (!input) return [];
  const metadataIds = sourceIdsFromMetadata(input.metadata);
  return metadataIds.length ? metadataIds : input.id ? [input.id] : [];
}

export function templateToolPolicySourceIds(policy: TemplateToolPolicy | TemplateToolPolicy[] | null): string[] {
  return templateToolPolicySettingsCore(policy).ids;
}

export function templateToolPolicyReferencePayload(
  aiInput: TemplateAiInput | null | undefined,
  policy: TemplateToolPolicy | TemplateToolPolicy[] | null,
): Record<string, unknown> {
  const aiInputIds = templateAiInputSourceIds(aiInput);
  const policyIds = templateToolPolicySourceIds(policy);
  return {
    ...(aiInputIds.length ? { template_ai_input_ids: aiInputIds } : {}),
    ...(policyIds.length ? { template_tool_policy_ids: policyIds } : {}),
  };
}

export function templateAiInputParamsPayload(
  aiInput: TemplateAiInput | null | undefined,
): Record<string, unknown> {
  return { ...(objectRecord(aiInput?.params) ?? {}) };
}

export function templateToolPolicySettings(policy: TemplateToolPolicy | TemplateToolPolicy[] | null) {
  return templateToolPolicySettingsCore(policy);
}

export function templateFeatureFlagEnabled(
  input: TemplateComposerInput | null | undefined,
  flagName: string,
  fallback = true,
): boolean {
  const flags = objectRecord(input?.feature_flags);
  const value = flags?.[flagName];
  return typeof value === "boolean" ? value : fallback;
}

function templateWidgetPayload(item: TemplateCatalogMetadataItem): Record<string, unknown> {
  return objectRecord(item.widget) ?? item;
}

function templateWidgetRefIds(input: TemplateAiInput | TemplateComposerInput | null): string[] {
  const raw = objectRecord(input)?.widgets;
  return stringList(raw);
}

function widgetAction(toolId: string): ComposerWidgetAction {
  return { type: "toggle_tool", tool_id: toolId };
}

export function templateComposerWidgetsForInput(
  catalog: UICatalog | null | undefined,
  aiInput: TemplateAiInput | null,
  composerInput: TemplateComposerInput | null,
  tools: ComposerExtensionItem[],
): DroppedWidget[] {
  const widgets = catalog?.composer_widgets ?? [];
  const requestedWidgetIds = new Set([
    ...templateWidgetRefIds(aiInput),
    ...templateWidgetRefIds(composerInput),
  ]);
  const toolById = new Map(tools.map((tool) => [tool.id, tool]));

  return widgets
    .filter((item) => item.enabled !== false)
    .filter((item) => requestedWidgetIds.size === 0 || (item.id && requestedWidgetIds.has(item.id)))
    .map<DroppedWidget | null>((item) => {
      const payload = templateWidgetPayload(item);
      const kind = nonEmptyString(payload.widgetKind) || nonEmptyString(payload.widget_kind) || nonEmptyString(item.widgetKind) || nonEmptyString(item.widget_kind);
      if (kind && kind !== "tool_toggle") return null;
      const toolId = (
        nonEmptyString(payload.tool_id)
        || nonEmptyString(payload.sourceItemId)
        || nonEmptyString(payload.source_item_id)
        || nonEmptyString(item.tool_id)
        || nonEmptyString(item.sourceItemId)
        || nonEmptyString(item.source_item_id)
      );
      const tool = toolId ? toolById.get(toolId) : null;
      if (!tool) return null;
      const label = nonEmptyString(payload.label) || nonEmptyString(item.label) || tool.ui?.composer_label || tool.label || tool.id;
      const description = nonEmptyString(payload.description) || nonEmptyString(item.description) || tool.ui?.composer_description || tool.description;
      const widget: DroppedWidget = {
        id: nonEmptyString(payload.id) || item.id || tool.id,
        type: "tool",
        label,
        description,
        enabled: payload.enabled !== false,
        widgetKind: "tool_toggle",
        action: widgetAction(tool.id),
        sourceItemId: tool.id,
        icon: nonEmptyString(payload.icon) || tool.ui?.composer_icon || tool.ui?.item_icon || tool.ui?.group_icon,
        metadata: {
          source: "template_catalog_widget",
          template_id: item.template_id ?? null,
          piece_id: item.piece_id ?? null,
          widget_id: item.id ?? null,
          tool: {
            id: tool.id,
            label: tool.label,
            category: tool.category ?? null,
            tags: tool.tags ?? [],
          },
        },
      };
      return widget;
    })
    .filter((widget): widget is DroppedWidget => widget !== null);
}
