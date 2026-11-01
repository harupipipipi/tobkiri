import { useLayoutEffect, useRef, useState, type ReactNode, type RefObject } from "react";

import { LayerPortal } from "./LayerPortal";
import { viewportPopoverPosition } from "./viewportPopoverPosition";

/** A modal-layer popover which escapes scrolling ancestors and follows its anchor. */
export function ViewportPopover({ anchorRef, onClose, children, className = "", label = "close provider select", open = true, preferredPlacement = "auto", desiredWidth = 520, maxHeight = 360, closeOnEscape = true }: {
  anchorRef: RefObject<HTMLElement | null>;
  onClose: () => void;
  children: ReactNode;
  className?: string;
  label?: string;
  open?: boolean;
  preferredPlacement?: "auto" | "above" | "below";
  desiredWidth?: number;
  maxHeight?: number;
  closeOnEscape?: boolean;
}) {
  const panelRef = useRef<HTMLDivElement>(null);
  const closeRef = useRef(onClose);
  closeRef.current = onClose;
  const [position, setPosition] = useState<ReturnType<typeof viewportPopoverPosition> | null>(null);
  useLayoutEffect(() => {
    if (!open) return;
    const anchor = anchorRef.current;
    const panel = panelRef.current;
    if (!anchor || !panel) return;
    const update = () => {
      const viewport = window.visualViewport;
      setPosition(viewportPopoverPosition(anchor.getBoundingClientRect(), {
        left: viewport?.offsetLeft ?? 0,
        top: viewport?.offsetTop ?? 0,
        width: viewport?.width ?? window.innerWidth,
        height: viewport?.height ?? window.innerHeight,
      }, desiredWidth, Math.min(maxHeight, panel.scrollHeight || maxHeight), preferredPlacement));
    };
    const escape = (event: KeyboardEvent) => {
      if (!closeOnEscape || event.key !== "Escape" || event.isComposing) return;
      event.preventDefault();
      event.stopPropagation();
      closeRef.current();
      anchor.focus();
    };
    update();
    window.addEventListener("resize", update);
    window.addEventListener("scroll", update, true);
    window.visualViewport?.addEventListener("resize", update);
    window.visualViewport?.addEventListener("scroll", update);
    document.addEventListener("keydown", escape, true);
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(update);
    observer?.observe(anchor);
    observer?.observe(panel);
    return () => {
      window.removeEventListener("resize", update);
      window.removeEventListener("scroll", update, true);
      window.visualViewport?.removeEventListener("resize", update);
      window.visualViewport?.removeEventListener("scroll", update);
      document.removeEventListener("keydown", escape, true);
      observer?.disconnect();
      if (panel.contains(document.activeElement) || document.activeElement === document.body) anchor.focus();
    };
  }, [anchorRef, open, desiredWidth, maxHeight, preferredPlacement, closeOnEscape]);
  const positioned = position !== null;
  useLayoutEffect(() => {
    if (!open || !positioned) return;
    const panel = panelRef.current;
    if (!panel || panel.contains(document.activeElement)) return;
    panel.querySelector<HTMLElement>("input:not([disabled]), button:not([disabled]):not([tabindex='-1']), select:not([disabled]), textarea:not([disabled]), [tabindex='0']")?.focus();
  }, [open, positioned]);
  if (!open || typeof document === "undefined") return null;
  return (
    <LayerPortal layer="modal">
      <button type="button" aria-label={label} tabIndex={-1} className="fixed inset-0 cursor-default" onClick={() => { closeRef.current(); anchorRef.current?.focus(); }} />
      <div ref={panelRef} data-viewport-popover onKeyDown={(event) => {
        if (event.key === "Tab") event.stopPropagation();
        if (event.defaultPrevented || event.nativeEvent.isComposing) return;
        if (event.key === "Escape") {
          event.preventDefault(); event.stopPropagation(); closeRef.current(); anchorRef.current?.focus();
        }
        if (event.key !== "Tab") return;
        const focusable = Array.from(event.currentTarget.querySelectorAll<HTMLElement>(
          "button:not([disabled]):not([tabindex='-1']), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex='0']",
        )).filter((element) => element.offsetParent !== null);
        event.stopPropagation();
        const first = focusable[0]; const last = focusable[focusable.length - 1];
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
      }} className={`fixed overflow-y-auto overscroll-contain ${className}`} style={position ? { left: position.left, top: position.top, width: position.width, maxHeight: position.maxHeight } : { visibility: "hidden" }}>
        {children}
      </div>
    </LayerPortal>
  );
}
