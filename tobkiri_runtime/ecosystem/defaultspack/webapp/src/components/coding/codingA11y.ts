import type { KeyboardEvent as ReactKeyboardEvent } from "react";

export function nextHorizontalTab<T extends string>(
  tabs: readonly T[],
  current: T,
  key: string,
): T | null {
  if (tabs.length === 0) return null;
  const currentIndex = Math.max(0, tabs.indexOf(current));
  if (key === "Home") return tabs[0] ?? null;
  if (key === "End") return tabs[tabs.length - 1] ?? null;
  if (key === "ArrowRight") return tabs[(currentIndex + 1) % tabs.length] ?? null;
  if (key === "ArrowLeft") return tabs[(currentIndex - 1 + tabs.length) % tabs.length] ?? null;
  return null;
}

export function handleHorizontalTabKey<T extends string>(
  event: ReactKeyboardEvent<HTMLButtonElement>,
  tabs: readonly T[],
  current: T,
  onActivate: (tab: T) => void,
  elementId: (tab: T) => string,
): void {
  const next = nextHorizontalTab(tabs, current, event.key);
  if (next === null) return;
  event.preventDefault();
  onActivate(next);
  globalThis.requestAnimationFrame?.(() => document.getElementById(elementId(next))?.focus());
}
