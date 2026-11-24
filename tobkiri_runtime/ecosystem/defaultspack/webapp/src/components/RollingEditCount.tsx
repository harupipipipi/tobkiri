import { useEffect, useLayoutEffect, useRef, useState } from "react";

export interface RollingEditCountProps {
  value: number | null;
  animateInitial?: boolean;
}

export interface RollingEditCountFrame {
  from: string | null;
  to: string | null;
  direction: "up" | "down";
  animate: boolean;
  revision: number;
}

/** Invalid or unconfirmed counts stay distinct from a confirmed zero. */
export function editCountText(value: number | null): string | null {
  return value !== null && Number.isFinite(value) && value >= 0
    ? Math.trunc(value).toLocaleString("en-US", { useGrouping: false }) : null;
}

/** Latest-value controller: stale animation completions cannot replace newer counts. */
export class RollingEditCountController {
  frame: RollingEditCountFrame;
  private initialRevealUsed = false;

  constructor(value: number | null) {
    const text = editCountText(value);
    this.frame = { from: text, to: text, direction: "up", animate: false, revision: 0 };
  }

  /** Reveal a real receipt's confirmed digits once, without inventing an old count. */
  revealInitial(motionAllowed: boolean): RollingEditCountFrame {
    if (this.initialRevealUsed) return this.frame;
    this.initialRevealUsed = true;
    if (!motionAllowed || this.frame.to === null) return this.frame;
    this.frame = {
      ...this.frame, from: "", animate: true, direction: "up",
      revision: this.frame.revision + 1,
    };
    return this.frame;
  }

  /** Animate actual updates; receipt mode can reveal an unknown count from blank. */
  update(value: number | null, motionAllowed: boolean, revealConfirmed = false): RollingEditCountFrame {
    const text = editCountText(value);
    const previous = this.frame.to;
    if (text === previous) return this.frame;
    const animate = motionAllowed && text !== null && (previous !== null || revealConfirmed);
    this.frame = {
      from: animate ? previous ?? "" : text,
      to: text,
      direction: Number(text) < Number(previous) ? "down" : "up",
      animate,
      revision: this.frame.revision + 1,
    };
    return this.frame;
  }

  /** Commit only the currently running animation's target. */
  settle(revision: number): RollingEditCountFrame {
    if (revision !== this.frame.revision || !this.frame.animate) return this.frame;
    this.frame = { ...this.frame, from: this.frame.to, animate: false };
    return this.frame;
  }

  /** Visibility and reduced-motion changes immediately settle the latest target. */
  stop(): RollingEditCountFrame {
    this.frame = {
      ...this.frame, from: this.frame.to, animate: false,
      revision: this.frame.revision + 1,
    };
    return this.frame;
  }
}

/** Right-aligned digit cells handle carry, shrinkage and decreasing counts. */
export function editCountColumns(frame: RollingEditCountFrame): {
  place: number; from: string; to: string; moving: boolean;
}[] {
  const target = frame.to ?? "—";
  const previous = frame.animate ? frame.from ?? target : target;
  const length = Math.max(previous.length, target.length);
  const from = previous.padStart(length, " ");
  const to = target.padStart(length, " ");
  return Array.from({ length }, (_, index) => ({
    place: length - index - 1,
    from: from[index],
    to: to[index],
    moving: frame.animate && from[index] !== to[index],
  }));
}

function motionAllowed(): boolean {
  return typeof document !== "undefined" && !document.hidden
    && typeof window !== "undefined" && typeof window.matchMedia === "function"
    && !window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

/** Compact odometer for confirmed line counts; the parent supplies color and prefix. */
export function RollingEditCount({ value, animateInitial = false }: RollingEditCountProps) {
  const controllerRef = useRef<RollingEditCountController | null>(null);
  if (controllerRef.current === null) controllerRef.current = new RollingEditCountController(value);
  const controller = controllerRef.current;
  const [frame, setFrame] = useState(() => controller.frame);
  const trackRefs = useRef(new Map<number, HTMLSpanElement>());
  const animationsRef = useRef<Animation[]>([]);
  const mountedRef = useRef(false);

  function cancelAnimations(): void {
    for (const animation of animationsRef.current) animation.cancel();
    animationsRef.current = [];
  }

  useLayoutEffect(() => {
    let next: RollingEditCountFrame;
    if (!mountedRef.current) {
      mountedRef.current = true;
      next = animateInitial ? controller.revealInitial(motionAllowed())
        : controller.update(value, motionAllowed());
    } else {
      next = controller.update(value, motionAllowed(), animateInitial);
    }
    setFrame(next);
  }, [animateInitial, controller, value]);

  useEffect(() => {
    const query = typeof window.matchMedia === "function"
      ? window.matchMedia("(prefers-reduced-motion: reduce)") : null;
    const stopWhenNeeded = () => {
      if (motionAllowed()) return;
      cancelAnimations();
      setFrame(controller.stop());
    };
    query?.addEventListener("change", stopWhenNeeded);
    document.addEventListener("visibilitychange", stopWhenNeeded);
    stopWhenNeeded();
    return () => {
      query?.removeEventListener("change", stopWhenNeeded);
      document.removeEventListener("visibilitychange", stopWhenNeeded);
      cancelAnimations();
    };
  }, [controller]);

  useLayoutEffect(() => {
    cancelAnimations();
    if (!frame.animate) return;
    let cancelled = false;
    const complete = () => {
      if (!cancelled) setFrame(controller.settle(frame.revision));
    };
    if (!motionAllowed()) { complete(); return; }
    try {
      for (const column of editCountColumns(frame)) {
        if (!column.moving) continue;
        const track = trackRefs.current.get(column.place);
        if (!track || typeof track.animate !== "function") {
          cancelAnimations();
          complete();
          return;
        }
        const transforms = frame.direction === "up"
          ? ["translateY(0)", "translateY(-50%)"]
          : ["translateY(-50%)", "translateY(0)"];
        animationsRef.current.push(track.animate(
          transforms.map((transform) => ({ transform })),
          { duration: 240, easing: "cubic-bezier(0.2, 0.7, 0.2, 1)", fill: "forwards" },
        ));
      }
      void Promise.all(animationsRef.current.map((animation) => animation.finished))
        .then(complete, complete);
    } catch {
      cancelAnimations();
      complete();
    }
    return () => {
      cancelled = true;
      cancelAnimations();
    };
  }, [controller, frame]);

  const accessibleText = editCountText(value) ?? "未確認";
  return <span className="inline-flex shrink-0 items-baseline font-mono tabular-nums"
    data-edit-count={editCountText(value) ?? "unconfirmed"}>
    <span className="sr-only">{accessibleText}</span>
    <span aria-hidden="true" className="inline-flex justify-end"
      style={{ minWidth: "3ch", height: "1em", lineHeight: 1, fontVariantNumeric: "tabular-nums" }}>
      {editCountColumns(frame).map((column) => {
        const rows = !column.moving ? [column.to]
          : frame.direction === "up" ? [column.from, column.to] : [column.to, column.from];
        return <span key={column.place} className="inline-block shrink-0 overflow-hidden"
          style={{ width: "1ch", height: "1em" }}>
          <span ref={(node) => {
            if (node) trackRefs.current.set(column.place, node);
            else trackRefs.current.delete(column.place);
          }} className="block" style={{
            transform: column.moving && frame.direction === "up" ? "translateY(-50%)" : "translateY(0)",
          }}>
            {rows.map((digit, index) => <span key={index} className="block"
              style={{ height: "1em", lineHeight: 1 }}>{digit === " " ? "\u00a0" : digit}</span>)}
          </span>
        </span>;
      })}
    </span>
  </span>;
}
