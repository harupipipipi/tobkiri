import type { ConversationSearchResult, SidebarItem, ModelSearchItem } from "./api";
import { shortcutLabel, shortcutSpecMatchesEvent } from "./keyboardShortcuts";

export type SpotlightCategory = "chat" | "widget" | "tool";
export type SpotlightResult =
  | { kind: "chat"; id: string; title: string; conversation: ConversationSearchResult }
  | { kind: "model"; id: string; title: string; model: ModelSearchItem }
  | { kind: "widget" | "tool"; id: string; title: string; description?: string; badge?: string | null };

/** Search registered sidebar screens, preserving catalog readiness rather than inventing tools. */
export function spotlightSidebarResults(
  items: SidebarItem[], category: SpotlightCategory, text: string,
  options: { enabled: boolean; disabledToolIds: Set<string> },
): Array<Extract<SpotlightResult, { kind: "widget" | "tool" }>> {
  if (!options.enabled || category === "chat") return [];
  const needle = text.toLocaleLowerCase();
  return items.filter((item) => item.category === category && item.id.trim())
    .filter((item) => [item.label, item.id, item.description, ...(item.tags ?? [])]
      .some((value) => value?.toLocaleLowerCase().includes(needle)))
    .map((item) => ({
      kind: category, id: item.id, title: item.label || item.id, description: item.description,
      badge: options.disabledToolIds.has(item.id) ? "Disabled"
        : item.tool_info?.setup_state?.status === "missing" ? "Unavailable" : item.badge,
    }));
}

type ShortcutEvent = Parameters<typeof shortcutSpecMatchesEvent>[1] & { keyCode?: number };

/** Respect IME, consumed events and settings; add the default macOS Command chord. */
export function spotlightShortcutMatchesEvent(
  shortcut: string, event: ShortcutEvent,
  options: { allowTextInput?: boolean; isMac?: boolean } = {},
): boolean {
  if (event.defaultPrevented || event.repeat || event.isComposing || event.keyCode === 229) return false;
  return shortcutSpecMatchesEvent(shortcut, event, options)
    || (options.isMac === true && shortcutLabel(shortcut) === "Ctrl+K"
      && shortcutSpecMatchesEvent("Cmd+K", event, options));
}

/** Show the same platform-specific chords that the Spotlight listener accepts. */
export function spotlightShortcutLabel(shortcut: string, isMac: boolean): string {
  const label = shortcutLabel(shortcut);
  if (isMac && label === "Ctrl+K") return "Ctrl+K / Cmd+K";
  return isMac ? label.replace(/\bWin\b/g, "Cmd") : label;
}
