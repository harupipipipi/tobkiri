/** Browser-local display preferences; these never grant permission to run a tool. */
export interface SidebarRailLayout {
  version: 1;
  order: string[];
  visibility: Record<string, boolean>;
}

export interface SidebarRailItem {
  id: string;
  defaultVisible?: boolean;
}

export interface RailLayoutStorage {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
}

export const REMOVED_RAIL_IDS = new Set([
  "starred-tools", "starred_tools", "__starred_tools__",
]);

/** Keep the first available slot and discard obsolete starred-tools entries. */
export function uniqueRailItems<T extends SidebarRailItem>(items: readonly T[]): T[] {
  const seen = new Set<string>();
  return items.filter((item) => {
    if (!item.id || REMOVED_RAIL_IDS.has(item.id) || seen.has(item.id)) return false;
    seen.add(item.id);
    return true;
  });
}

/** Reconcile saved display preferences with the slots available in this render. */
export function normalizeSidebarRailLayout(
  raw: unknown,
  items: readonly SidebarRailItem[],
): SidebarRailLayout {
  const available = uniqueRailItems(items);
  const ids = new Set(available.map((item) => item.id));
  const candidate = raw && typeof raw === "object"
    ? raw as Record<string, unknown> : {};
  const supported = candidate.version === 1;
  const order: string[] = [];
  if (supported && Array.isArray(candidate.order)) {
    for (const id of candidate.order) {
      if (typeof id === "string" && ids.has(id) && !order.includes(id)) order.push(id);
    }
  }
  for (const item of available) if (!order.includes(item.id)) order.push(item.id);
  const savedVisibility = supported && candidate.visibility
    && typeof candidate.visibility === "object"
    && !Array.isArray(candidate.visibility)
    ? candidate.visibility as Record<string, unknown> : {};
  const visibility = Object.fromEntries(available.map((item) => [
    item.id,
    Object.prototype.hasOwnProperty.call(savedVisibility, item.id)
      && typeof savedVisibility[item.id] === "boolean"
      ? savedVisibility[item.id] as boolean : item.defaultVisible !== false,
  ]));
  return { version: 1, order, visibility };
}

/** Use an explicit local profile scope so profiles do not share rail preferences. */
export function sidebarRailStorageKey(scope = "default"): string {
  return `tobkiri:right-sidebar-rail-layout:v1:${encodeURIComponent(scope)}`;
}

/** Read without repairing or writing storage during hydration. */
export function readSidebarRailLayout(
  storage: RailLayoutStorage,
  scope: string | undefined,
  items: readonly SidebarRailItem[],
): { layout: SidebarRailLayout; preferences: SidebarRailLayout; error?: string } {
  try {
    const raw = storage.getItem(sidebarRailStorageKey(scope));
    const parsed: unknown = raw ? JSON.parse(raw) : null;
    const layout = normalizeSidebarRailLayout(parsed, items);
    const record = parsed && typeof parsed === "object"
      ? parsed as Record<string, unknown> : {};
    const saved = record.version === 1 && record.visibility
      && typeof record.visibility === "object" && !Array.isArray(record.visibility)
      ? record.visibility as Record<string, unknown> : {};
    const overrides = Object.fromEntries(Object.keys(saved)
      .filter((id) => !REMOVED_RAIL_IDS.has(id) && typeof saved[id] === "boolean")
      .map((id) => [id, saved[id] as boolean]));
    const order = record.version === 1 && Array.isArray(record.order)
      ? [...new Set(record.order.filter((id): id is string => typeof id === "string"
        && !!id && !REMOVED_RAIL_IDS.has(id)))] : [];
    return { layout, preferences: { version: 1, order, visibility: overrides } };
  } catch {
    return {
      layout: normalizeSidebarRailLayout(null, items),
      preferences: { version: 1, order: [], visibility: {} },
      error: "保存済みの表示設定を読み込めませんでした。既定の表示を使用しています。",
    };
  }
}

/** Report success only after the local storage write completes. */
export function saveSidebarRailLayout(
  storage: RailLayoutStorage,
  scope: string | undefined,
  layout: SidebarRailLayout,
): { ok: true } | { ok: false; error: string } {
  try {
    storage.setItem(sidebarRailStorageKey(scope), JSON.stringify(layout));
    return { ok: true };
  } catch {
    return { ok: false, error: "端末に保存できませんでした。変更はこの画面のみ有効です。" };
  }
}

/** Move an existing slot to an existing slot's position without dropping hidden slots. */
export function moveSidebarRailItem(
  order: readonly string[],
  id: string,
  targetId: string,
): string[] {
  const from = order.indexOf(id);
  const to = order.indexOf(targetId);
  if (from < 0 || to < 0 || from === to) return [...order];
  const next = [...order];
  next.splice(from, 1);
  next.splice(to, 0, id);
  return next;
}

/** Cancellation and unchanged gestures must not write a new layout. */
export function sidebarRailOrderChanged(
  before: readonly string[],
  after: readonly string[],
): boolean {
  return before.length !== after.length || before.some((id, index) => id !== after[index]);
}

/** Pointer controller shared by mouse, pen and touch; it never persists a layout. */
export class SidebarRailPointerGesture {
  private pointer: { id: number; x: number; y: number; active: boolean } | null = null;
  suppressClick = false;

  /** Track only the primary left-button contact and keep taps as ordinary clicks. */
  begin(event: { pointerId: number; clientX: number; clientY: number; isPrimary: boolean; button: number }): boolean {
    if (this.pointer || !event.isPrimary || event.button !== 0) return false;
    this.pointer = { id: event.pointerId, x: event.clientX, y: event.clientY, active: false };
    return true;
  }

  /** Activate a reorder only after the contact moves at least six CSS pixels. */
  move(event: { pointerId: number; clientX: number; clientY: number }): boolean {
    const pointer = this.pointer;
    if (!pointer || pointer.id !== event.pointerId) return false;
    if (!pointer.active && Math.hypot(event.clientX - pointer.x, event.clientY - pointer.y) >= 6) {
      pointer.active = true;
      this.suppressClick = true;
    }
    return pointer.active;
  }

  /** Cancel, lost capture and no-op drops never authorize a storage write. */
  finish(cancelled: boolean, changed: boolean): boolean {
    const shouldCommit = !!this.pointer?.active && !cancelled && changed;
    this.pointer = null;
    return shouldCommit;
  }

  /** Consume the one synthetic click which can follow a drag or lost capture. */
  consumeClick(): boolean {
    const suppressed = this.suppressClick;
    this.suppressClick = false;
    return suppressed;
  }
}

/** Preserve asynchronously unavailable catalogue preferences when saving a new order. */
export function sidebarRailPreferenceOrder(
  preferences: SidebarRailLayout,
  items: readonly SidebarRailItem[],
): string[] {
  return [...new Set([...preferences.order, ...uniqueRailItems(items).map((item) => item.id)])]
    .filter((id) => !!id && !REMOVED_RAIL_IDS.has(id));
}
