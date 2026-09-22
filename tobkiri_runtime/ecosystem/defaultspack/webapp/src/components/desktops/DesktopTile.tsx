import { AlertTriangle, Bot, Circle, Keyboard, Monitor, UserCheck } from "lucide-react";
import { useEffect, useRef, useState, type ClipboardEvent, type CompositionEvent, type KeyboardEvent, type MouseEvent, type PointerEvent, type WheelEvent } from "react";

import { cn } from "../../lib/cn";
import type { DesktopInputAction, DesktopInstance } from "../../features/sandboxes/types";
import { useDesktopFrame } from "../../features/sandboxes/useDesktopFrames";
import { ErrorNotice } from "../ErrorNotice";
import { pointerToDesktopCoordinates } from "./desktopCoordinates";
import { DesktopControlSurface } from "./DesktopControlSurface";

const MOVE_THROTTLE_MS = 50;
const DRAG_THRESHOLD_PX = 4;

type DesktopPointerButton = "left" | "middle" | "right";

type PointerSession = {
  pointerId: number;
  viewX: number;
  viewY: number;
  desktopX: number;
  desktopY: number;
  button: DesktopPointerButton;
};

type DesktopTileProps = {
  desktop: DesktopInstance;
  selected: boolean;
  dense?: boolean;
  prominent?: boolean;
  hasLease: boolean;
  accessKey?: string | null;
  controlBusy?: boolean;
  onSelect: (seatId: string) => void;
  onTakeOver: () => void;
  onReturnToAI: () => void;
  onInput: (input: DesktopInputAction) => void;
  onStart: () => void;
  onRestart: () => void;
  onStop: () => void;
  onDelete: () => void;
};

function statusTone(status: string): string {
  if (status === "running") return "text-emerald-300";
  if (status === "provisioning" || status === "starting" || status === "creating") return "text-amber-300";
  if (status === "failed") return "text-red-300";
  return "text-zinc-500";
}

function frameAgeLabel(ageMs: number | null): string {
  if (ageMs === null) return "No frame";
  if (ageMs < 1000) return "now";
  if (ageMs < 60000) return `${Math.round(ageMs / 1000)}s ago`;
  return `${Math.round(ageMs / 60000)}m ago`;
}

function pointerButton(button: number): DesktopPointerButton {
  if (button === 1) return "middle";
  if (button === 2) return "right";
  return "left";
}

function desktopKey(event: KeyboardEvent<HTMLDivElement>): string | null {
  const map: Record<string, string> = {
    ArrowDown: "Down",
    ArrowLeft: "Left",
    ArrowRight: "Right",
    ArrowUp: "Up",
    Backspace: "BackSpace",
    Delete: "Delete",
    End: "End",
    Enter: "Return",
    Escape: "Escape",
    Home: "Home",
    PageDown: "Page_Down",
    PageUp: "Page_Up",
    Tab: "Tab",
    " ": "space",
  };
  return map[event.key] ?? null;
}

function desktopKeyCombo(event: KeyboardEvent<HTMLDivElement>): string | null {
  const key = desktopKey(event) ?? (event.key.length === 1 ? event.key.toLowerCase() : null);
  if (!key) return null;
  const modifiers = [
    event.ctrlKey ? "ctrl" : null,
    event.altKey ? "alt" : null,
    event.metaKey ? "super" : null,
    event.shiftKey && event.key.length > 1 ? "shift" : null,
  ].filter(Boolean);
  return [...modifiers, key].join("+");
}

type KeyboardCaptureDecision =
  | { kind: "release" }
  | { kind: "ignore" }
  | { kind: "type"; text: string }
  | { kind: "key"; key: string };

export function keyboardCaptureDecision(event: {
  key: string;
  ctrlKey: boolean;
  altKey: boolean;
  metaKey: boolean;
  shiftKey: boolean;
  isComposing?: boolean;
}): KeyboardCaptureDecision {
  if (event.key === "Escape") return { kind: "release" };
  if (event.isComposing || event.key === "Process" || event.key === "Dead") return { kind: "ignore" };
  if (event.key.length === 1 && !event.metaKey && !event.ctrlKey && !event.altKey) {
    return { kind: "type", text: event.key };
  }
  const key = desktopKeyCombo(event as KeyboardEvent<HTMLDivElement>);
  return key ? { kind: "key", key } : { kind: "ignore" };
}

export function DesktopTile({
  desktop,
  selected,
  dense = false,
  prominent = false,
  hasLease,
  accessKey,
  controlBusy = false,
  onSelect,
  onTakeOver,
  onReturnToAI,
  onInput,
  onStart,
  onRestart,
  onStop,
  onDelete,
}: DesktopTileProps) {
  const frameRegionRef = useRef<HTMLDivElement | null>(null);
  const keyboardControlButtonRef = useRef<HTMLButtonElement | null>(null);
  const [keyboardCaptured, setKeyboardCaptured] = useState(false);
  const pointerSessionRef = useRef<PointerSession | null>(null);
  const lastMoveRef = useRef(0);
  const { frame, error, ageMs, pollNow } = useDesktopFrame({
    seatId: desktop.seat_id,
    status: desktop.status,
    selected,
    hasControlLease: hasLease,
    accessKey,
  });
  const resolution = frame
    ? { width: frame.width, height: frame.height }
    : desktop.resolution ?? { width: 1280, height: 800 };
  const frameAspectRatio = `${Math.max(resolution.width, 1)} / ${Math.max(resolution.height, 1)}`;
  const provider = desktop.provider_label || desktop.provider_id || "provider pending";
  const controlLabel = hasLease
    ? "Human control"
    : desktop.control?.holder === "ai"
      ? "AI control"
      : "Control available";

  const releaseKeyboard = () => {
    setKeyboardCaptured(false);
    requestAnimationFrame(() => keyboardControlButtonRef.current?.focus());
  };

  useEffect(() => {
    if (keyboardCaptured && (!hasLease || desktop.status !== "running")) {
      releaseKeyboard();
    }
  }, [desktop.status, hasLease, keyboardCaptured]);

  const mapPointer = (event: PointerEvent<HTMLDivElement> | MouseEvent<HTMLDivElement> | WheelEvent<HTMLDivElement>) => {
    if (!hasLease || !frame || !frameRegionRef.current) return;
    const rect = frameRegionRef.current.getBoundingClientRect();
    return pointerToDesktopCoordinates(
      { x: event.clientX - rect.left, y: event.clientY - rect.top },
      { width: rect.width, height: rect.height },
      { width: resolution.width, height: resolution.height },
    );
  };

  const handlePointerDown = (event: PointerEvent<HTMLDivElement>) => {
    const mapped = mapPointer(event);
    if (!mapped) return;
    event.preventDefault();
    frameRegionRef.current?.focus();
    frameRegionRef.current?.setPointerCapture(event.pointerId);
    pointerSessionRef.current = {
      pointerId: event.pointerId,
      viewX: event.clientX,
      viewY: event.clientY,
      desktopX: mapped.desktopX,
      desktopY: mapped.desktopY,
      button: pointerButton(event.button),
    };
    onInput({ action: "move", x: mapped.desktopX, y: mapped.desktopY });
  };

  const handlePointerMove = (event: PointerEvent<HTMLDivElement>) => {
    const mapped = mapPointer(event);
    if (!mapped) return;
    const now = Date.now();
    if (now - lastMoveRef.current < MOVE_THROTTLE_MS) return;
    lastMoveRef.current = now;
    onInput({ action: "move", x: mapped.desktopX, y: mapped.desktopY });
  };

  const handlePointerUp = (event: PointerEvent<HTMLDivElement>) => {
    const session = pointerSessionRef.current;
    const mapped = mapPointer(event);
    if (!session || !mapped) return;
    event.preventDefault();
    pointerSessionRef.current = null;
    if (frameRegionRef.current?.hasPointerCapture(event.pointerId)) {
      frameRegionRef.current.releasePointerCapture(event.pointerId);
    }
    const viewDistance = Math.hypot(event.clientX - session.viewX, event.clientY - session.viewY);
    if (viewDistance > DRAG_THRESHOLD_PX) {
      onInput({
        action: "drag",
        x: session.desktopX,
        y: session.desktopY,
        to_x: mapped.desktopX,
        to_y: mapped.desktopY,
        button: session.button,
      });
      return;
    }
    onInput({ action: "click", x: mapped.desktopX, y: mapped.desktopY, button: session.button });
  };

  const handlePointerCancel = (event: PointerEvent<HTMLDivElement>) => {
    pointerSessionRef.current = null;
    if (frameRegionRef.current?.hasPointerCapture(event.pointerId)) {
      frameRegionRef.current.releasePointerCapture(event.pointerId);
    }
  };

  const handleDoubleClick = (event: MouseEvent<HTMLDivElement>) => {
    const mapped = mapPointer(event);
    if (!mapped) return;
    event.preventDefault();
    onInput({ action: "double_click", x: mapped.desktopX, y: mapped.desktopY, button: "left" });
  };

  const handleWheel = (event: WheelEvent<HTMLDivElement>) => {
    const mapped = mapPointer(event);
    if (!mapped) return;
    const deltaY = Math.max(-20, Math.min(20, Math.trunc(event.deltaY / 60) || (event.deltaY > 0 ? 1 : -1)));
    const deltaX = Math.trunc(event.deltaX / 60);
    event.preventDefault();
    onInput({ action: "scroll", x: mapped.desktopX, y: mapped.desktopY, delta_x: deltaX, delta_y: deltaY });
  };

  const handleKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (!hasLease || !keyboardCaptured) return;
    const decision = keyboardCaptureDecision({
      key: event.key,
      ctrlKey: event.ctrlKey,
      altKey: event.altKey,
      metaKey: event.metaKey,
      shiftKey: event.shiftKey,
      isComposing: event.nativeEvent.isComposing,
    });
    if (decision.kind === "release") {
      event.preventDefault();
      event.stopPropagation();
      releaseKeyboard();
      return;
    }
    if (decision.kind === "ignore") return;
    if (decision.kind === "type") {
      event.preventDefault();
      onInput({ action: "type_text", text: decision.text });
      return;
    }
    event.preventDefault();
    event.stopPropagation();
    onInput({ action: "key", key: decision.key });
  };

  const handlePaste = (event: ClipboardEvent<HTMLDivElement>) => {
    if (!hasLease || !keyboardCaptured) return;
    const text = event.clipboardData.getData("text");
    if (!text) return;
    event.preventDefault();
    onInput({ action: "type_text", text });
  };

  const handleCompositionEnd = (event: CompositionEvent<HTMLDivElement>) => {
    if (!hasLease || !keyboardCaptured || !event.data) return;
    event.preventDefault();
    onInput({ action: "type_text", text: event.data });
  };

  return (
    <article
      className={cn(
        "group flex min-h-[280px] flex-col rounded-lg border bg-[#0a0a0c] transition-colors",
        selected ? "border-zinc-500 text-zinc-100" : "border-zinc-800/70 text-zinc-300 hover:border-zinc-700",
        dense && "min-h-[238px]",
        prominent && "min-h-[calc(100vh-150px)]",
      )}
      data-testid={`desktop-tile-${desktop.seat_id}`}
    >
      <button
        type="button"
        onClick={() => onSelect(desktop.seat_id)}
        aria-current={selected ? "page" : undefined}
        className="flex min-h-12 items-center justify-between gap-2 border-b border-zinc-800/70 px-3 py-2 text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-zinc-500/70"
      >
        <div className="flex min-w-0 items-center gap-2">
          <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md border border-zinc-800 bg-zinc-950 text-zinc-300">
            <Monitor size={15} />
          </span>
          <div className="min-w-0">
            <p className="truncate text-sm font-semibold">{desktop.name}</p>
            <p className="truncate text-[11px] text-zinc-500">{provider}</p>
          </div>
        </div>
        <span className={cn("flex shrink-0 items-center gap-1 text-[11px] font-medium", statusTone(desktop.status))}>
          <Circle size={9} fill="currentColor" />
          {desktop.status}
        </span>
      </button>

      {hasLease && (
        <div className="mx-3 mt-3 flex flex-wrap items-center justify-between gap-2 rounded-md border border-zinc-800 bg-zinc-950/70 px-3 py-2">
          <div id={`desktop-keyboard-help-${desktop.seat_id}`} role="status" aria-live="polite" className="min-w-0 text-[11px] text-zinc-400">
            <span className="font-medium text-zinc-200">{keyboardCaptured ? `Keyboard control active for ${desktop.name}.` : `Keyboard control is off for ${desktop.name}.`}</span>{" "}
            {keyboardCaptured ? "Press Escape or Ctrl+Alt+Shift+Escape to release. Tab and shortcuts are sent remotely." : "Start explicitly to send typing, paste, IME text, navigation keys, and shortcuts remotely."}
          </div>
          <button
            ref={keyboardControlButtonRef}
            type="button"
            aria-pressed={keyboardCaptured}
            disabled={desktop.status !== "running"}
            onClick={() => {
              if (keyboardCaptured) {
                releaseKeyboard();
              } else {
                setKeyboardCaptured(true);
                requestAnimationFrame(() => frameRegionRef.current?.focus());
              }
            }}
            className="flex h-8 shrink-0 items-center gap-1.5 rounded-md border border-zinc-700 bg-zinc-900 px-2 text-[11px] font-medium text-zinc-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-zinc-400"
          >
            <Keyboard size={13} />
            {keyboardCaptured ? "Release keyboard control" : "Start keyboard control"}
          </button>
        </div>
      )}

      <div
        ref={frameRegionRef}
        onPointerDown={handlePointerDown}
        onPointerMove={handlePointerMove}
        onPointerUp={handlePointerUp}
        onPointerCancel={handlePointerCancel}
        onDoubleClick={handleDoubleClick}
        onWheel={handleWheel}
        onKeyDown={handleKeyDown}
        onPaste={handlePaste}
        onCompositionEnd={handleCompositionEnd}
        onContextMenu={(event) => {
          if (hasLease) event.preventDefault();
        }}
        tabIndex={hasLease ? 0 : -1}
        className={cn(
          "relative m-3 flex min-h-[154px] items-center justify-center overflow-hidden rounded-md border border-zinc-800 bg-black",
          hasLease ? "cursor-crosshair" : "cursor-default",
          keyboardCaptured && "ring-2 ring-emerald-400/80",
          dense && "min-h-[128px]",
          prominent && "m-2 min-h-[520px] flex-1",
        )}
        style={{ aspectRatio: frameAspectRatio }}
        role={keyboardCaptured ? "application" : "group"}
        aria-label={`${desktop.name} live snapshot${keyboardCaptured ? ", keyboard control active" : ""}`}
        aria-describedby={hasLease ? `desktop-keyboard-help-${desktop.seat_id}` : undefined}
      >
        {frame ? (
          <img src={frame.object_url} alt="" className="h-full w-full object-contain" draggable={false} />
        ) : (
          <div className="flex flex-col items-center gap-2 text-zinc-600">
            {desktop.status === "failed" ? <AlertTriangle size={24} /> : <Monitor size={24} />}
            <span className="text-xs">{desktop.status === "running" ? "Waiting for first snapshot" : desktop.status}</span>
          </div>
        )}
        {error && (
          <ErrorNotice
            className="absolute inset-x-2 bottom-2 bg-red-950/90 px-2 py-1 text-[11px]"
            copyLabel="デスクトップ表示エラーをコピー"
            message={error}
          />
        )}
      </div>

      <div className="grid gap-2 px-3 pb-3">
        <div className="flex flex-wrap items-center justify-between gap-2 text-[11px] text-zinc-500">
          <span className="flex items-center gap-1">
            {hasLease ? <UserCheck size={12} className="text-zinc-300" /> : <Bot size={12} />}
            {controlLabel}
          </span>
          <span>Last frame {frameAgeLabel(ageMs ?? desktop.frame?.age_ms ?? null)}</span>
        </div>
        <DesktopControlSurface
          desktop={desktop}
          hasLease={hasLease}
          busy={controlBusy}
          onTakeOver={onTakeOver}
          onReturnToAI={onReturnToAI}
          onSnapshot={() => void pollNow()}
          onStart={onStart}
          onRestart={onRestart}
          onStop={onStop}
          onDelete={onDelete}
        />
      </div>
    </article>
  );
}
