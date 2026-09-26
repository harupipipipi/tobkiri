import type { KeyboardEvent, RefCallback } from "react";
import { useCallback, useEffect, useMemo, useRef } from "react";

export type AdaptiveTabOrientation = "horizontal" | "vertical";

/** Return the next tab index for the APG automatic-activation keyboard model. */
export function nextAdaptiveTabIndex(
  key: string,
  currentIndex: number,
  count: number,
  orientation: AdaptiveTabOrientation,
): number | null {
  if (count <= 0 || currentIndex < 0) return null;
  if (key === "Home") return 0;
  if (key === "End") return count - 1;

  const previousKey = orientation === "vertical" ? "ArrowUp" : "ArrowLeft";
  const nextKey = orientation === "vertical" ? "ArrowDown" : "ArrowRight";
  if (key === previousKey) return (currentIndex - 1 + count) % count;
  if (key === nextKey) return (currentIndex + 1) % count;
  return null;
}

function safeDomId(value: string): string {
  const sanitized = value.replace(/[^A-Za-z0-9_-]/g, "-");
  if (sanitized === value) return value;
  let hash = 2166136261;
  for (const character of value) {
    hash ^= character.codePointAt(0) ?? 0;
    hash = Math.imul(hash, 16777619);
  }
  return `${sanitized}-${(hash >>> 0).toString(36)}`;
}

/** Bind a controlled tab set to stable relationships and roving keyboard focus. */
export function useAdaptiveTabs<T extends string>({
  ids,
  selectedId,
  onSelect,
  idPrefix,
  orientation = "horizontal",
}: {
  ids: readonly T[];
  selectedId: T | null;
  onSelect: (id: T) => void;
  idPrefix: string;
  orientation?: AdaptiveTabOrientation;
}) {
  const tabRefs = useRef(new Map<T, HTMLButtonElement>());
  const tabRefCallbacks = useRef(new Map<T, RefCallback<HTMLButtonElement>>());
  const focusedTabId = useRef<T | null>(null);
  const restoreFocus = useRef(false);
  const activeId = ids.includes(selectedId as T) ? selectedId : (ids[0] ?? null);
  const idsKey = ids.join("\u0000");

  useEffect(() => {
    if (activeId && activeId !== selectedId) onSelect(activeId);
  }, [activeId, onSelect, selectedId]);

  useEffect(() => {
    const focusedId = focusedTabId.current;
    if ((!focusedId || ids.includes(focusedId)) && !restoreFocus.current) return;
    if (!activeId) return;
    restoreFocus.current = false;
    focusedTabId.current = activeId;
    tabRefs.current.get(activeId)?.focus();
  }, [activeId, idsKey]);

  const tabId = useCallback(
    (id: T) => `${idPrefix}-tab-${safeDomId(id)}`,
    [idPrefix],
  );
  const panelId = useCallback(
    (id: T) => `${idPrefix}-panel-${safeDomId(id)}`,
    [idPrefix],
  );
  const registerTab = useCallback(
    (id: T): RefCallback<HTMLButtonElement> => {
      const existing = tabRefCallbacks.current.get(id);
      if (existing) return existing;
      const callback: RefCallback<HTMLButtonElement> = (node) => {
        if (node) {
          tabRefs.current.set(id, node);
          return;
        }
        const previousNode = tabRefs.current.get(id);
        if (previousNode && previousNode === document.activeElement) {
          restoreFocus.current = true;
        }
        tabRefs.current.delete(id);
      };
      tabRefCallbacks.current.set(id, callback);
      return callback;
    },
    [],
  );
  const selectAndFocus = useCallback(
    (id: T) => {
      onSelect(id);
      focusedTabId.current = id;
      tabRefs.current.get(id)?.focus();
    },
    [onSelect],
  );

  return useMemo(() => ({
    activeId,
    panelId,
    tabId,
    tabProps: (id: T) => ({
      id: tabId(id),
      role: "tab" as const,
      "aria-selected": activeId === id,
      "aria-controls": panelId(id),
      tabIndex: activeId === id ? 0 : -1,
      ref: registerTab(id),
      onClick: () => onSelect(id),
      onFocus: () => {
        focusedTabId.current = id;
      },
      onBlur: (event: { relatedTarget: EventTarget | null }) => {
        if (![...tabRefs.current.values()].includes(event.relatedTarget as HTMLButtonElement)) {
          focusedTabId.current = null;
        }
      },
      onKeyDown: (event: KeyboardEvent<HTMLButtonElement>) => {
        const currentIndex = ids.indexOf(id);
        const nextIndex = nextAdaptiveTabIndex(
          event.key,
          currentIndex,
          ids.length,
          orientation,
        );
        if (nextIndex === null) return;
        event.preventDefault();
        const nextId = ids[nextIndex];
        if (nextId) selectAndFocus(nextId);
      },
    }),
  }), [activeId, ids, onSelect, orientation, panelId, registerTab, selectAndFocus, tabId]);
}
