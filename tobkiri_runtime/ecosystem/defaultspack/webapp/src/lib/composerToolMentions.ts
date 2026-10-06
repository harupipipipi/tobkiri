import type { ComposerExtensionItem, DroppedWidget, ToolGroup } from "../renderers/types";
import type { ConversationToolPreferences, ToolSelectionMode, ToolSelectionRequest, ToolTarget } from "../features/tools/types";
import { composerMentionMetadataFromWidgets, composerServiceMentionWidget, composerToolMentionWidget } from "./composerWidgets";
import { codePointIndexToUtf16Offset, extractMentionTokens, hasUnescapedMentionSyntax } from "./mentionContract";
import { toolGroupFor } from "./toolUi";

export type ComposerToolMentionDraft = {
  include: ToolTarget[];
  exclude: ToolTarget[];
  toolIds: string[];
  widgets: DroppedWidget[];
};

/** Consume a startup snapshot even when the current draft was already migrated. */
export function consumeLegacyToolMentionSnapshot(
  snapshot: { current: string[] },
  migrationKey: string,
  completedMigrations: string[],
): string[] {
  const selectedIds = snapshot.current;
  snapshot.current = [];
  return completedMigrations.includes(migrationKey) ? [] : selectedIds;
}

function servicesFor(items: ComposerExtensionItem[]) {
  const services = new Map<string, { id: string; label: string; toolIds: string[]; canonical: boolean }>();
  for (const item of items.filter((candidate) => !candidate.disabled)) {
    const group = toolGroupFor(item);
    const service = services.get(group.id) ?? { id: group.id, label: group.label, toolIds: [], canonical: false };
    service.toolIds.push(item.id);
    services.set(group.id, service);
  }
  // Wire service ids come from the authenticated catalog, never presentation groups.
  const canonical = new Map<string, { id: string; label: string; toolIds: string[]; canonical: boolean }>();
  for (const item of items.filter((candidate) => !candidate.disabled && candidate.ui?.service_id)) {
    const id = item.ui!.service_id!;
    const group = toolGroupFor(item);
    const service = canonical.get(id) ?? { id, label: group.id === id ? group.label : id, toolIds: [], canonical: true };
    service.toolIds.push(item.id);
    canonical.set(id, service);
  }
  for (const [id, service] of canonical) services.set(id, service);
  return [...services.values()];
}

/** Present authenticated services alongside exact-tool presentation groups. */
export function composerToolMentionGroups(items: ComposerExtensionItem[]): ToolGroup[] {
  return servicesFor(items).map((service) => ({
    id: service.id,
    label: service.label,
    description: service.canonical ? "接続されたサービス" : "ツールのグループ",
    items: items.filter((item) => !item.disabled && service.toolIds.includes(item.id)),
  }));
}

function targetWidget(target: ToolTarget, items: ComposerExtensionItem[], exclude = false): DroppedWidget | null {
  const syntax = `@${exclude ? "-" : ""}${target.id}`;
  const item = items.find((candidate) => candidate.id === target.id && !candidate.disabled);
  const service = servicesFor(items).find((candidate) => candidate.id === target.id);
  const widget = target.kind === "tool" && item ? composerToolMentionWidget(item, syntax)
    : target.kind === "service" && service ? composerServiceMentionWidget(service) : null;
  if (!widget) return null;
  return {
    ...widget,
    id: exclude ? `exclude:${widget.id}` : widget.id,
    metadata: {
      ...widget.metadata,
      mention: { ...(widget.metadata?.mention as Record<string, unknown>), syntax, intent: exclude ? "exclude" : "include" },
    },
  };
}

/** Resolve confirmed widgets whose visible syntax and catalog target remain valid. */
export function resolveComposerToolMentions(text: string, widgets: DroppedWidget[], items: ComposerExtensionItem[]): ComposerToolMentionDraft {
  const available = items.filter((item) => !item.disabled);
  const services = servicesFor(available);
  const include = new Map<string, ToolTarget>();
  const exclude = new Map<string, ToolTarget>();
  const activeWidgets: DroppedWidget[] = [];
  const add = (target: ToolTarget, negative: boolean, widget?: DroppedWidget | null) => {
    (negative ? exclude : include).set(`${target.kind}:${target.id}`, target);
    if (widget) activeWidgets.push(widget);
  };
  for (const widget of widgets) {
    for (const mention of composerMentionMetadataFromWidgets([widget])) {
      if ((mention.kind !== "tool" && mention.kind !== "service") || widget.enabled === false
        || !hasUnescapedMentionSyntax(text, mention.syntax)) continue;
      const target = { kind: mention.kind, id: mention.id };
      if (!targetWidget(target, available)) continue;
      const record = widget.metadata?.mention as Record<string, unknown>;
      add(target, record.intent === "exclude" || mention.syntax.startsWith("@-"), widget);
    }
  }
  const excludedIds = new Set([...exclude.values()].flatMap((target) => target.kind === "tool" ? [target.id]
    : services.find((service) => service.id === target.id)?.toolIds ?? []));
  const toolIds = [...new Set([...include.values()].flatMap((target) => target.kind === "tool" ? [target.id]
    : services.find((service) => service.id === target.id)?.toolIds ?? []))].filter((id) => !excludedIds.has(id));
  // Semantic widgets take precedence over synthesized spelling widgets with the same id.
  const byId = new Map<string, DroppedWidget>();
  for (const widget of activeWidgets) if (!byId.has(widget.id)) byId.set(widget.id, widget);
  const wireTargets = (targets: ToolTarget[], negative = false) => {
    const expanded = targets.flatMap((target): ToolTarget[] => {
      if (target.kind === "tool") return [target];
      const service = services.find((candidate) => candidate.id === target.id);
      // Includes bind to exactly the admitted visible members. A wire service include
      // would expand hidden/unavailable registry members and disagree with the editor.
      return negative && service?.canonical ? [target]
        : (service?.toolIds ?? []).map((id) => ({ kind: "tool", id }));
    });
    return [...new Map(expanded.map((target) => [`${target.kind}:${target.id}`, target])).values()];
  };
  return { include: wireTargets([...include.values()]), exclude: wireTargets([...exclude.values()], true), toolIds, widgets: [...byId.values()] };
}

/** Build an authoritative turn request while keeping the existing wire schema. */
export function composerMentionSelectionRequest(draft: ComposerToolMentionDraft, mode: ToolSelectionMode): ToolSelectionRequest {
  const effectiveMode = mode === "auto" && draft.toolIds.length ? "manual" : mode;
  return {
    mode: effectiveMode,
    include: effectiveMode === "none" ? [] : draft.include,
    exclude: draft.exclude,
    scope: "turn",
    must_use: effectiveMode === "manual" && draft.toolIds.length > 0,
  };
}

/** Materialize old pins/local selections once; persisted preferences remain untouched. */
export function materializeLegacyToolMentions(text: string, preferences: ConversationToolPreferences, selectedIds: string[], items: ComposerExtensionItem[]) {
  let value = text;
  const widgets: DroppedWidget[] = [];
  const targets = [
    ...(preferences.include ?? []).map((target) => ({ target, exclude: false })),
    ...selectedIds.map((id) => ({ target: { kind: "tool" as const, id }, exclude: false })),
    ...(preferences.exclude ?? []).map((target) => ({ target, exclude: true })),
  ];
  for (const { target, exclude } of targets) {
    const widget = targetWidget(target, items, exclude);
    if (!widget) continue; // Unavailable entries never become active references.
    const syntax = `@${exclude ? "-" : ""}${target.id}`;
    if (!hasUnescapedMentionSyntax(value, syntax)) value = `${value}${value && !/\s$/.test(value) ? " " : ""}${syntax} `;
    widgets.push(widget);
  }
  return { value, widgets };
}

/** Sidebar/batch selection edits the same input source of truth as the @ picker. */
export function replaceComposerToolMentions(text: string, widgets: DroppedWidget[], items: ComposerExtensionItem[], selectedIds: string[]) {
  const draft = resolveComposerToolMentions(text, widgets, items);
  const ranges: Array<{ start: number; end: number }> = [];
  for (const widget of draft.widgets) {
    const record = widget.metadata?.mention as Record<string, unknown>;
    if (record.intent === "exclude") {
      const affected = record.kind === "tool" ? [String(record.id)]
        : servicesFor(items).find((service) => service.id === record.id)?.toolIds ?? [];
      if (!affected.some((id) => selectedIds.includes(id))) continue;
    }
    const syntax = String(record.syntax);
    for (let start = text.indexOf(syntax); start >= 0; start = text.indexOf(syntax, start + 1)) {
      // Checking a suffix alone would incorrectly match emails and escaped @ signs.
      if (!extractMentionTokens(text, [syntax.slice(1)]).some((token) => codePointIndexToUtf16Offset(text, token.start) === start)) continue;
      const next = text.slice(start + syntax.length, start + syntax.length + 1);
      if (next && /[\p{L}\p{M}\p{N}_/:-]/u.test(next)) continue;
      ranges.push({ start, end: start + syntax.length });
    }
  }
  let value = text;
  const uniqueRanges = [...new Map(ranges.map((range) => [`${range.start}:${range.end}`, range])).values()];
  for (const range of uniqueRanges.sort((a, b) => b.start - a.start)) value = value.slice(0, range.start) + value.slice(range.end);
  const remaining = widgets.filter((widget) => !composerMentionMetadataFromWidgets([widget]).some((mention) => mention.kind === "tool" || mention.kind === "service"));
  const next = materializeLegacyToolMentions(value, {}, selectedIds, items);
  return { value: next.value, widgets: [...remaining, ...draft.widgets.filter((widget) => {
    const record = widget.metadata?.mention as Record<string, unknown>;
    return record.intent === "exclude" && hasUnescapedMentionSyntax(next.value, String(record.syntax));
  }), ...next.widgets] };
}

/** Service references carry only selection, not unsupported custom context. */
export function isSavedTurnToolMentionWidget(widget: DroppedWidget): boolean {
  return (widget.type === "tool" && widget.widgetKind === "tool_toggle")
    || (widget.type === "service" && widget.metadata?.source === "composer_at_mention");
}
