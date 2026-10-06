import { HISTORY_REFERENCE_DROP_EVENT, type HistoryReferenceDragPayload } from "./historyReferences";

export type HistoryDropPoint = { x: number; y: number };

/** Only a visible composer target can receive a pointer reference drop. */
export function historyReferenceTargetAtPoint(point: HistoryDropPoint): string | null {
  if (!Number.isFinite(point.x) || !Number.isFinite(point.y)
      || point.x < 0 || point.y < 0 || point.x >= window.innerWidth
      || point.y >= window.innerHeight) return null;
  const element = document.elementsFromPoint(point.x, point.y)[0];
  const target = element?.closest<HTMLElement>('[data-history-reference-drop-target="composer"]');
  if (!target?.isConnected || !target.id || target.id.length > 256) return null;
  const rect = target.getBoundingClientRect();
  if (![rect.left, rect.right, rect.top, rect.bottom].every(Number.isFinite)
      || rect.right <= rect.left || rect.bottom <= rect.top
      || point.x < rect.left || point.x >= rect.right
      || point.y < rect.top || point.y >= rect.bottom) return null;
  const style = window.getComputedStyle(target);
  if (style.display === "none" || style.visibility === "hidden"
      || style.visibility === "collapse" || style.opacity === "0") return null;
  return target.id;
}

/** Emit a display candidate after the caller has restored its history snapshot. */
export function dispatchHistoryReferenceDrop(payload: HistoryReferenceDragPayload, point: HistoryDropPoint, targetId: string): void {
  window.dispatchEvent(new CustomEvent(HISTORY_REFERENCE_DROP_EVENT, {
    detail: { rawPayload: JSON.stringify(payload), point, targetId },
  }));
}
