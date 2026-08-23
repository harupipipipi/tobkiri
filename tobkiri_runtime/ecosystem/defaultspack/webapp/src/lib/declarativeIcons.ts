import { BellRing, type LucideIcon } from "lucide-react";

const DECLARATIVE_ICON_ALIASES: Readonly<Record<string, LucideIcon>> = Object.freeze({
  bell: BellRing,
  "bell-ring": BellRing,
  notification: BellRing,
  notifications: BellRing,
  notify: BellRing,
});

/** Normalize a bounded declarative icon alias without evaluating pack content. */
export function normalizeDeclarativeIconName(value: unknown): string {
  return String(value ?? "")
    .trim()
    .toLowerCase()
    .replace(/[\s_]+/g, "-")
    .replace(/-+/g, "-");
}

/** Resolve an allowlisted declarative icon alias, or return null for fallback. */
export function declarativeIconForName(value: unknown): LucideIcon | null {
  return DECLARATIVE_ICON_ALIASES[normalizeDeclarativeIconName(value)] ?? null;
}
