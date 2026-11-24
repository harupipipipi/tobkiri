import {
  Children, Fragment, isValidElement, useEffect, useId, useRef, useState,
  type DragEventHandler, type KeyboardEvent, type PointerEvent,
  type ReactElement, type ReactNode, type Ref,
} from "react";
import { createPortal } from "react-dom";
import { GripVertical, Settings2, X } from "lucide-react";
import { layerClassName } from "../ui/layers/layerTokens";
import {
  moveSidebarRailItem, normalizeSidebarRailLayout, readSidebarRailLayout,
  saveSidebarRailLayout, sidebarRailOrderChanged, uniqueRailItems,
  SidebarRailPointerGesture, sidebarRailPreferenceOrder, type SidebarRailLayout,
} from "../lib/sidebarRailLayout";

export interface RailSlotProps {
  id: string;
  label: string;
  category: string;
  icon: ReactNode;
  defaultVisible?: boolean;
  available?: boolean;
  children: ReactNode;
}

/** Declarative metadata around an existing action; it does not change that action. */
export function RailSlot({ children }: RailSlotProps): ReactNode {
  return children;
}

/** Extract slots through arrays and fragments, retaining the first available ID. */
export function collectRailSlots(children: ReactNode): RailSlotProps[] {
  const slots: RailSlotProps[] = [];
  const visit = (nodes: ReactNode) => Children.forEach(nodes, (child) => {
    if (!isValidElement(child)) return;
    if (child.type === RailSlot) slots.push((child as ReactElement<RailSlotProps>).props);
    else if (child.type === Fragment) visit((child.props as { children: ReactNode }).children);
  });
  visit(children);
  return uniqueRailItems(slots);
}

export interface CustomizableSidebarRailProps {
  children: ReactNode;
  storageScope?: string;
  onDragOver?: DragEventHandler<HTMLDivElement>;
  onDrop?: DragEventHandler<HTMLDivElement>;
  keyboardButtonNavigation?: boolean;
  scrollRef?: Ref<HTMLDivElement>;
}

type Surface = "rail" | "customize";
interface ReorderSession {
  id: string;
  surface: Surface;
  original: SidebarRailLayout;
  focus: HTMLElement | null;
  pointerId?: number;
  startX?: number;
  startY?: number;
  x?: number;
  y?: number;
  active: boolean;
  capture?: HTMLElement;
  nativeDraggable?: boolean;
}

const instructions = "並べ替えハンドルで Space または Enter を押すと移動を開始します。矢印・Home・End で移動、Enter で確定、Escape で取消。ドラッグでも並べ替えできます。";

/** A local display editor that leaves original action handlers and approval paths intact. */
export function CustomizableSidebarRail({
  children, storageScope, onDragOver, onDrop, keyboardButtonNavigation = true,
  scrollRef,
}: CustomizableSidebarRailProps): ReactElement {
  const slots = collectRailSlots(children);
  const slotsRef = useRef(slots);
  slotsRef.current = slots;
  const [preferences, setPreferences] = useState<SidebarRailLayout>(() => ({
    version: 1, order: [], visibility: {},
  }));
  const preferencesRef = useRef(preferences);
  preferencesRef.current = preferences;
  const layout = normalizeSidebarRailLayout(preferences, slots);
  const [open, setOpen] = useState(false);
  const [notice, setNotice] = useState("");
  const [announcement, setAnnouncement] = useState("");
  const [movingId, setMovingId] = useState<string | null>(null);
  const railRef = useRef<HTMLDivElement>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const dialogRef = useRef<HTMLDivElement>(null);
  const customizeRef = useRef<HTMLButtonElement>(null);
  const sessionRef = useRef<ReorderSession | null>(null);
  const frameRef = useRef<number | null>(null);
  const focusFrameRef = useRef<number | null>(null);
  const pointerGestureRef = useRef(new SidebarRailPointerGesture());
  const suppressTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const instructionId = useId();
  const titleId = useId();
  const tabIndex = keyboardButtonNavigation ? 0 : -1;

  function update(next: SidebarRailLayout): void {
    preferencesRef.current = next;
    setPreferences(next);
  }

  function stopFrame(): void {
    if (frameRef.current !== null) cancelAnimationFrame(frameRef.current);
    frameRef.current = null;
  }

  function restoreFocus(session: ReorderSession): void {
    if (focusFrameRef.current !== null) cancelAnimationFrame(focusFrameRef.current);
    focusFrameRef.current = requestAnimationFrame(() => {
      focusFrameRef.current = null;
      if (session.focus?.isConnected) session.focus.focus({ preventScroll: true });
    });
  }

  function cancelReorder(shouldRestoreFocus = true): void {
    const session = sessionRef.current;
    if (!session) return;
    sessionRef.current = null;
    stopFrame();
    if (session.pointerId !== undefined && session.capture?.hasPointerCapture?.(session.pointerId)) {
      session.capture.releasePointerCapture(session.pointerId);
    }
    pointerGestureRef.current.finish(true, false);
    update(session.original);
    setMovingId(null);
    if (session.active) setAnnouncement("並べ替えを取り消しました。");
    if (session.active && session.pointerId !== undefined) {
      if (suppressTimerRef.current) clearTimeout(suppressTimerRef.current);
      suppressTimerRef.current = setTimeout(() => { pointerGestureRef.current.suppressClick = false; }, 0);
    }
    if (shouldRestoreFocus && session.active) restoreFocus(session);
  }

  function persist(next: SidebarRailLayout): void {
    update(next);
    try {
      const result = saveSidebarRailLayout(window.localStorage, storageScope, next);
      setNotice(result.ok ? "" : result.error);
      setAnnouncement(result.ok ? "端末に表示設定を保存しました。" : result.error);
    } catch {
      const error = "端末に保存できませんでした。変更はこの画面のみ有効です。";
      setNotice(error);
      setAnnouncement(error);
    }
  }

  function finishReorder(): void {
    const session = sessionRef.current;
    if (!session) return;
    sessionRef.current = null;
    stopFrame();
    if (session.pointerId !== undefined && session.capture?.hasPointerCapture?.(session.pointerId)) {
      session.capture.releasePointerCapture(session.pointerId);
    }
    setMovingId(null);
    const changed = sidebarRailOrderChanged(session.original.order, preferencesRef.current.order);
    const commit = session.pointerId === undefined ? session.active && changed
      : pointerGestureRef.current.finish(false, changed);
    if (commit) {
      persist(preferencesRef.current);
    } else if (session.active) {
      setAnnouncement("順序は変更されていません。");
    }
    if (session.active) restoreFocus(session);
  }

  useEffect(() => {
    cancelReorder();
    try {
      const loaded = readSidebarRailLayout(window.localStorage, storageScope, slotsRef.current);
      update(loaded.preferences);
      setNotice(loaded.error ?? "");
    } catch {
      update({ version: 1, order: [], visibility: {} });
      setNotice("保存済みの表示設定を読み込めませんでした。既定の表示を使用しています。");
    }
    // Scope changes reset local preferences; slot default changes remain dynamic.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [storageScope]);

  useEffect(() => {
    const cancel = () => cancelReorder();
    const hidden = () => { if (document.hidden) cancel(); };
    window.addEventListener("blur", cancel);
    const escape = (event: globalThis.KeyboardEvent) => {
      if (event.key === "Escape" && sessionRef.current) { event.preventDefault(); cancelReorder(); }
    };
    document.addEventListener("keydown", escape);
    document.addEventListener("visibilitychange", hidden);
    const outside = (event: globalThis.PointerEvent) => {
      const target = event.target as Node;
      return !railRef.current?.contains(target) && !listRef.current?.contains(target);
    };
    const move = (event: globalThis.PointerEvent) => {
      if (outside(event)) pointerMove(event);
    };
    const finish = (event: globalThis.PointerEvent) => {
      if (outside(event)) pointerEnd(event, false);
    };
    const abort = (event: globalThis.PointerEvent) => {
      if (outside(event)) pointerEnd(event, true);
    };
    window.addEventListener("pointermove", move, { passive: false });
    window.addEventListener("pointerup", finish);
    window.addEventListener("pointercancel", abort);
    return () => {
      window.removeEventListener("blur", cancel);
      document.removeEventListener("keydown", escape);
      document.removeEventListener("visibilitychange", hidden);
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", finish);
      window.removeEventListener("pointercancel", abort);
      stopFrame();
      if (focusFrameRef.current !== null) cancelAnimationFrame(focusFrameRef.current);
      if (suppressTimerRef.current) clearTimeout(suppressTimerRef.current);
      sessionRef.current = null;
      pointerGestureRef.current.finish(true, false);
    };
    // Session and layout are read from refs to avoid restarting active gestures.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    const session = sessionRef.current;
    if (session) {
      const slot = slots.find((item) => item.id === session.id);
      if (!slot || (session.surface === "rail"
        && (slot.available === false || !layout.visibility[slot.id]))) cancelReorder();
    }
  });

  useEffect(() => {
    if (!open) return;
    dialogRef.current?.querySelector<HTMLButtonElement>("button")?.focus();
    const dismiss = (event: globalThis.PointerEvent) => {
      if (!dialogRef.current?.contains(event.target as Node)
        && !customizeRef.current?.contains(event.target as Node)) closeDialog();
    };
    document.addEventListener("pointerdown", dismiss);
    return () => document.removeEventListener("pointerdown", dismiss);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  function closeDialog(): void {
    cancelReorder();
    setOpen(false);
    customizeRef.current?.focus({ preventScroll: true });
  }

  function announcePosition(id: string, order: string[]): void {
    const slot = slotsRef.current.find((item) => item.id === id);
    const availableOrder = normalizeSidebarRailLayout({ version: 1, order, visibility: {} }, slotsRef.current).order;
    setAnnouncement(`${slot?.label ?? id}: ${availableOrder.indexOf(id) + 1} / ${availableOrder.length}`);
  }

  function moveTo(id: string, targetId: string): void {
    const fullOrder = sidebarRailPreferenceOrder(preferencesRef.current, slotsRef.current);
    const nextOrder = moveSidebarRailItem(fullOrder, id, targetId);
    if (!sidebarRailOrderChanged(fullOrder, nextOrder)) return;
    update({ ...preferencesRef.current, order: nextOrder });
    announcePosition(id, nextOrder);
    if (sessionRef.current?.pointerId === undefined && sessionRef.current) {
      restoreFocus(sessionRef.current);
    }
  }

  function keyReorder(event: KeyboardEvent<HTMLButtonElement>, id: string, surface: Surface): void {
    let session = sessionRef.current;
    if (!session) {
      if (event.key !== " " && event.key !== "Enter") return;
      event.preventDefault();
      const current = normalizeSidebarRailLayout(preferencesRef.current, slotsRef.current);
      // Retain only explicit visibility overrides while snapshotting the full order.
      session = { id, surface, active: true,
        original: { ...preferencesRef.current, order: sidebarRailPreferenceOrder(preferencesRef.current, slotsRef.current) }, focus: event.currentTarget };
      sessionRef.current = session;
      update(session.original);
      setMovingId(id);
      setAnnouncement(`${slotsRef.current.find((slot) => slot.id === id)?.label}: 移動を開始。${instructions}`);
      return;
    }
    if (session.id !== id || session.pointerId !== undefined) return;
    if (!["Escape", "Enter", " ", "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    if (event.key === "Escape") { cancelReorder(); return; }
    if (event.key === "Enter" || event.key === " ") { finishReorder(); return; }
    const current = normalizeSidebarRailLayout(preferencesRef.current, slotsRef.current);
    const category = slotsRef.current.find((slot) => slot.id === id)?.category;
    const candidates = surface === "rail"
      ? current.order.filter((itemId) => current.visibility[itemId]
        && slotsRef.current.find((slot) => slot.id === itemId)?.available !== false)
      : current.order.filter((itemId) => slotsRef.current.find((slot) => slot.id === itemId)?.category === category);
    const index = candidates.indexOf(id);
    const targetIndex = event.key === "Home" ? 0 : event.key === "End" ? candidates.length - 1
      : index + (["ArrowUp", "ArrowLeft"].includes(event.key) ? -1 : 1);
    const target = candidates[Math.max(0, Math.min(candidates.length - 1, targetIndex))];
    if (target) moveTo(id, target);
  }

  function reorderAtPointer(): void {
    const session = sessionRef.current;
    if (!session?.active || session.y === undefined) return;
    const container = session.surface === "rail" ? railRef.current : listRef.current;
    const rows = container?.querySelectorAll<HTMLElement>("[data-rail-slot-id]");
    if (!rows?.length) return;
    let nearest: HTMLElement | null = null;
    let distance = Infinity;
    const category = slotsRef.current.find((slot) => slot.id === session.id)?.category;
    rows.forEach((row) => {
      if (session.surface === "customize"
        && slotsRef.current.find((slot) => slot.id === row.dataset.railSlotId)?.category !== category) return;
      const bounds = row.getBoundingClientRect();
      const nextDistance = Math.abs(session.y! - (bounds.top + bounds.height / 2));
      if (nextDistance < distance) { nearest = row; distance = nextDistance; }
    });
    const target = (nearest as HTMLElement | null)?.dataset.railSlotId;
    if (target) moveTo(session.id, target);
  }

  function autoscroll(): void {
    const session = sessionRef.current;
    if (!session?.active || session.pointerId === undefined) { stopFrame(); return; }
    const container = session.surface === "rail" ? railRef.current : listRef.current;
    if (container && session.y !== undefined) {
      const bounds = container.getBoundingClientRect();
      const edge = Math.min(40, bounds.height / 4);
      const topDistance = session.y - bounds.top;
      const bottomDistance = bounds.bottom - session.y;
      const speed = topDistance < edge ? -Math.min(12, (edge - topDistance) / 3)
        : bottomDistance < edge ? Math.min(12, (edge - bottomDistance) / 3) : 0;
      if (speed) { container.scrollTop += speed; reorderAtPointer(); }
    }
    frameRef.current = requestAnimationFrame(autoscroll);
  }

  function pointerDown(event: PointerEvent<HTMLElement>, id: string, surface: Surface): void {
    if (!event.currentTarget.contains(event.target as Node)
      || sessionRef.current || !pointerGestureRef.current.begin(event)) return;
    const current = normalizeSidebarRailLayout(preferencesRef.current, slotsRef.current);
    sessionRef.current = { id, surface, pointerId: event.pointerId,
      startX: event.clientX, startY: event.clientY, x: event.clientX, y: event.clientY,
      original: { ...preferencesRef.current, order: sidebarRailPreferenceOrder(preferencesRef.current, slotsRef.current) }, active: false,
      focus: document.activeElement instanceof HTMLElement ? document.activeElement : null,
      capture: event.currentTarget,
      nativeDraggable: !!(event.target as Element).closest("[draggable=true]") };
  }

  function pointerMove(event: Pick<PointerEvent<HTMLElement>, "pointerId" | "clientX" | "clientY" | "preventDefault">): void {
    const session = sessionRef.current;
    if (!session || session.pointerId !== event.pointerId) return;
    session.x = event.clientX;
    session.y = event.clientY;
    if (!session.active) {
      if (session.nativeDraggable
        && Math.abs(event.clientX - session.startX!) > Math.abs(event.clientY - session.startY!)) return;
      if (!pointerGestureRef.current.move(event)) return;
      session.active = true;
      session.focus = document.activeElement instanceof HTMLElement ? document.activeElement : session.focus;
      session.capture?.setPointerCapture(event.pointerId);
      update(session.original);
      setMovingId(session.id);
      setAnnouncement("ドラッグで並べ替え中。離すと確定します。");
      frameRef.current = requestAnimationFrame(autoscroll);
    }
    event.preventDefault();
    reorderAtPointer();
  }

  function pointerEnd(event: Pick<PointerEvent<HTMLElement>, "pointerId" | "preventDefault">, cancelled: boolean): void {
    const session = sessionRef.current;
    if (!session || session.pointerId !== event.pointerId) return;
    const active = session.active;
    if (cancelled) cancelReorder(); else finishReorder();
    if (active) {
      event.preventDefault();
      // Browser click follows pointerup; clear only after that event has been consumed.
      if (suppressTimerRef.current) clearTimeout(suppressTimerRef.current);
      suppressTimerRef.current = setTimeout(() => { pointerGestureRef.current.suppressClick = false; }, 0);
    }
  }

  function handleDialogKey(event: KeyboardEvent<HTMLDivElement>): void {
    if (event.key === "Escape") {
      event.preventDefault();
      if (sessionRef.current) cancelReorder(); else closeDialog();
    }
    if (event.key !== "Tab") return;
    const focusable = dialogRef.current?.querySelectorAll<HTMLButtonElement>("button:not(:disabled)");
    if (!focusable?.length) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  }

  function reorderHandle(slot: RailSlotProps, surface: Surface): ReactElement {
    return <button type="button" tabIndex={surface === "rail" ? tabIndex : 0}
      data-rail-reorder-handle={slot.id}
      aria-label={`${slot.label}を並べ替え`} aria-describedby={instructionId}
      aria-pressed={movingId === slot.id}
      title={`${slot.label}を並べ替え`}
      className={surface === "rail"
        ? "absolute -right-0.5 top-0 flex h-5 w-3 items-center justify-center rounded text-zinc-500 opacity-0 hover:bg-zinc-700 hover:text-zinc-100 focus:opacity-100 group-hover:opacity-100 group-focus-within:opacity-100"
        : "flex h-8 w-6 shrink-0 items-center justify-center rounded text-zinc-500 hover:bg-zinc-800 hover:text-zinc-200 focus-visible:outline focus-visible:outline-sky-400"}
      style={{ touchAction: "none" }}
      onKeyDown={(event) => keyReorder(event, slot.id, surface)}
      onBlur={(event) => {
        if (sessionRef.current?.pointerId === undefined
          && !(focusFrameRef.current !== null && event.relatedTarget === null)) cancelReorder(false);
      }}
      onClick={(event) => { event.preventDefault(); event.stopPropagation(); }}>
      <GripVertical size={surface === "rail" ? 10 : 16} aria-hidden="true" />
    </button>;
  }

  const byId = new Map(slots.map((slot) => [slot.id, slot]));
  const ordered = layout.order.map((id) => byId.get(id)!).filter(Boolean);
  const categories = [...new Set(slots.map((slot) => slot.category))];
  const dialog = open && typeof document !== "undefined" ? createPortal(
    <>
    <div aria-hidden="true" className={`fixed inset-0 ${layerClassName.globalOverlay}`}
      onPointerDown={(event) => { event.preventDefault(); closeDialog(); }} />
    <div ref={dialogRef} role="dialog" aria-modal="true" aria-labelledby={titleId}
      onKeyDown={handleDialogKey}
      className={`fixed flex flex-col rounded-xl border border-zinc-700 bg-zinc-950 p-3 text-zinc-100 shadow-2xl ${layerClassName.globalOverlay}`}
      style={{ right: 12, bottom: 12,
        width: "min(340px, calc(100vw - 24px))", maxHeight: "calc(100dvh - 24px)" }}>
      <div className="mb-2 flex shrink-0 items-center justify-between gap-2">
        <h2 id={titleId} className="text-sm font-semibold">サイドバーをカスタマイズ</h2>
        <button type="button" onClick={closeDialog} aria-label="カスタマイズを閉じる"
          className="flex h-8 w-8 items-center justify-center rounded hover:bg-zinc-800 focus-visible:outline focus-visible:outline-sky-400"><X size={16} /></button>
      </div>
      <p className="mb-3 text-xs text-zinc-400">表示と順序をこの端末に保存します。<br />
        一覧ではカテゴリ内を並べ替え。カテゴリを越える移動はサイドバーで。</p>
      {notice && <p role="alert" className="mb-2 text-xs text-amber-300">{notice}</p>}
      <div ref={listRef} className="min-h-0 flex-1 overflow-y-auto overscroll-contain">
        {categories.map((category) => <section key={category} className="mb-3">
          <h3 className="mb-1 px-1 text-xs font-semibold text-zinc-400">{category}</h3>
          {ordered.filter((slot) => slot.category === category).map((slot) =>
            <div key={slot.id} data-rail-slot-id={slot.id}
              className={`flex min-h-11 items-center gap-2 rounded-lg px-1 ${movingId === slot.id ? "bg-sky-500/15 ring-1 ring-sky-500/30" : "hover:bg-zinc-900"}`}
              onPointerDown={(event) => {
                if ((event.target as Element).closest("[data-rail-reorder-handle]")) pointerDown(event, slot.id, "customize");
              }}
              onPointerMove={pointerMove} onPointerUp={(event) => pointerEnd(event, false)}
              onPointerCancel={(event) => pointerEnd(event, true)}
              onLostPointerCapture={() => { if (sessionRef.current?.pointerId !== undefined) cancelReorder(); }}>
              {reorderHandle(slot, "customize")}
              <span aria-hidden="true" className="flex h-5 w-5 shrink-0 items-center justify-center text-zinc-400">{slot.icon}</span>
              <span className="min-w-0 flex-1 break-words text-sm">{slot.label}
                <span className="block text-[10px] text-zinc-500">レール位置: {layout.order.indexOf(slot.id) + 1} / {layout.order.length}</span>
                {slot.available === false && <span className="block text-[10px] text-zinc-500">現在のフィルターでは非表示</span>}
              </span>
              <button type="button" role="switch" aria-checked={layout.visibility[slot.id]}
                aria-label={`${slot.label}を表示`}
                onClick={() => { cancelReorder(); persist({ ...preferencesRef.current,
                  order: sidebarRailPreferenceOrder(preferencesRef.current, slotsRef.current), visibility: { ...preferencesRef.current.visibility, [slot.id]: !layout.visibility[slot.id] } }); }}
                className={`relative ml-2 h-6 w-10 shrink-0 rounded-full focus-visible:outline focus-visible:outline-sky-400 ${layout.visibility[slot.id] ? "bg-sky-600" : "bg-zinc-700"}`}>
                <span aria-hidden="true" className={`absolute top-1 h-4 w-4 rounded-full bg-white ${layout.visibility[slot.id] ? "right-1" : "left-1"}`} />
              </button>
            </div>)}
        </section>)}
      </div>
    </div>
    </>, document.body) : null;

  return <div className="rumi-right-sidebar-rail relative flex h-full min-h-0 w-13 shrink-0 self-stretch flex-col overflow-visible">
    <p id={instructionId} className="sr-only">{instructions}</p>
    <div aria-live="polite" aria-atomic="true" className="sr-only">{announcement}</div>
    <div ref={(node) => {
      railRef.current = node;
      if (typeof scrollRef === "function") scrollRef(node);
      else if (scrollRef) scrollRef.current = node;
    }}
      className="rumi-right-sidebar-rail-scroll flex min-h-0 w-full flex-1 flex-col items-center gap-1 overflow-x-hidden overflow-y-auto overscroll-contain py-2 scrollbar-none"
      onDragOver={onDragOver} onDrop={onDrop}
      onDragStartCapture={(event) => {
        if (!railRef.current?.contains(event.target as Node)) return;
        const session = sessionRef.current;
        const vertical = session?.y !== undefined && session.startY !== undefined
          && Math.abs(session.y - session.startY) > 0
          && Math.abs(session.y - session.startY) >= Math.abs((session.x ?? 0) - (session.startX ?? 0));
        if (session?.active || vertical
          || (event.target as Element).closest("[data-rail-reorder-handle]")) event.preventDefault();
        else cancelReorder();
      }}
      onClickCapture={(event) => {
        if (railRef.current?.contains(event.target as Node) && pointerGestureRef.current.consumeClick()) { event.preventDefault(); event.stopPropagation(); }
      }}>
      {ordered.filter((slot) => slot.available !== false && layout.visibility[slot.id]).map((slot) =>
        <div key={slot.id} data-rail-slot-id={slot.id}
          className={`group relative flex w-full shrink-0 items-center justify-center ${movingId === slot.id ? "rounded-lg bg-sky-500/15 ring-1 ring-sky-500/30" : ""}`}
          style={{ touchAction: "none" }}
          onPointerDownCapture={(event) => pointerDown(event, slot.id, "rail")}
          onPointerMove={pointerMove} onPointerUp={(event) => pointerEnd(event, false)}
          onPointerCancel={(event) => pointerEnd(event, true)}
          onLostPointerCapture={() => { if (sessionRef.current?.pointerId !== undefined) cancelReorder(); }}>
          {slot.children}
          {reorderHandle(slot, "rail")}
        </div>)}
    </div>
    {notice && <span role="alert" className="shrink-0 px-1 text-center text-[10px] text-amber-300" title={notice}>保存設定エラー</span>}
    <div className="flex shrink-0 items-center justify-center border-t border-zinc-800 bg-zinc-950 py-2">
      <button ref={customizeRef} type="button" title="カスタマイズ" aria-label="カスタマイズ"
        aria-haspopup="dialog" aria-expanded={open} tabIndex={tabIndex}
        onClick={() => { cancelReorder(); setOpen((value) => !value); }}
        className="flex h-9 w-9 items-center justify-center rounded-xl text-zinc-400 hover:bg-zinc-800 hover:text-zinc-100 focus-visible:outline focus-visible:outline-sky-400">
        <Settings2 size={18} aria-hidden="true" />
      </button>
    </div>
    {dialog}
  </div>;
}
