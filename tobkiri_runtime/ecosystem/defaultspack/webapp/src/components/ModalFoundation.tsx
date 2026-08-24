import { useEffect, useId, useRef, type HTMLAttributes, type ReactNode, type RefObject } from "react";

export type ModalVariant = "dialog" | "alertdialog" | "drawer" | "trusted-window" | "popover";

const focusableSelector = [
  "button:not([disabled]):not([tabindex='-1'])",
  "[href]:not([tabindex='-1'])",
  "input:not([disabled]):not([tabindex='-1'])",
  "select:not([disabled]):not([tabindex='-1'])",
  "textarea:not([disabled]):not([tabindex='-1'])",
  "[tabindex]:not([tabindex='-1'])",
].join(",");

const modalStack: symbol[] = [];

export function modalStackTransition(stack: readonly string[], action: "open" | "close", id: string): string[] {
  if (action === "open") return [...stack.filter((item) => item !== id), id];
  return stack.filter((item) => item !== id);
}

function isTopLayer(token: symbol) {
  return modalStack.at(-1) === token;
}

function inertBackground(layer: HTMLElement): () => void {
  const changed: Array<{ element: HTMLElement; inert: boolean; ariaHidden: string | null }> = [];
  let current: HTMLElement | null = layer;
  while (current?.parentElement) {
    for (const sibling of Array.from(current.parentElement.children)) {
      if (!(sibling instanceof HTMLElement) || sibling === current || sibling.contains(current)) continue;
      changed.push({ element: sibling, inert: sibling.inert, ariaHidden: sibling.getAttribute("aria-hidden") });
      sibling.inert = true;
      sibling.setAttribute("aria-hidden", "true");
    }
    current = current.parentElement;
    if (current === document.body) break;
  }
  return () => {
    for (const { element, inert, ariaHidden } of changed) {
      element.inert = inert;
      if (ariaHidden === null) element.removeAttribute("aria-hidden");
      else element.setAttribute("aria-hidden", ariaHidden);
    }
  };
}

function useModalContract({
  layerRef,
  panelRef,
  onClose,
  dismissible,
  initialFocusRef,
}: {
  layerRef: RefObject<HTMLElement | null>;
  panelRef: RefObject<HTMLElement | null>;
  onClose: () => void;
  dismissible: boolean;
  initialFocusRef?: RefObject<HTMLElement | null>;
}) {
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;
  useEffect(() => {
    const layer = layerRef.current;
    const panel = panelRef.current;
    if (!layer || !panel) return undefined;
    const token = Symbol("modal-layer");
    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    modalStack.push(token);
    const restoreBackground = inertBackground(layer);
    const focusTarget = initialFocusRef?.current ?? panel.querySelector<HTMLElement>(focusableSelector) ?? panel;
    requestAnimationFrame(() => focusTarget.focus());

    const onKeyDown = (event: KeyboardEvent) => {
      if (!isTopLayer(token)) return;
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        if (dismissible) onCloseRef.current();
        return;
      }
      if (event.key !== "Tab") return;
      const focusable = Array.from(panel.querySelectorAll<HTMLElement>(focusableSelector)).filter((item) => !item.inert && item.getAttribute("aria-hidden") !== "true");
      if (focusable.length === 0) {
        event.preventDefault();
        panel.focus();
        return;
      }
      const first = focusable[0];
      const last = focusable.at(-1)!;
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", onKeyDown, true);
    return () => {
      document.removeEventListener("keydown", onKeyDown, true);
      const index = modalStack.lastIndexOf(token);
      if (index >= 0) modalStack.splice(index, 1);
      restoreBackground();
      requestAnimationFrame(() => opener?.isConnected && opener.focus());
    };
  }, [dismissible, initialFocusRef, layerRef, panelRef]);
}

export function ModalFoundation({
  variant = "dialog",
  title,
  description,
  onClose,
  dismissible = true,
  initialFocusRef,
  backdropClassName,
  panelClassName,
  children,
  ...panelProps
}: {
  variant?: ModalVariant;
  title: string;
  description?: string;
  onClose: () => void;
  dismissible?: boolean;
  initialFocusRef?: RefObject<HTMLElement | null>;
  backdropClassName?: string;
  panelClassName?: string;
  children: ReactNode;
} & Omit<HTMLAttributes<HTMLElement>, "title">) {
  const layerRef = useRef<HTMLDivElement | null>(null);
  const panelRef = useRef<HTMLElement | null>(null);
  const titleId = useId();
  const descriptionId = useId();
  const isPopover = variant === "popover";
  useModalContract({ layerRef, panelRef, onClose, dismissible, initialFocusRef });
  const role = variant === "alertdialog" ? "alertdialog" : "dialog";

  return (
    <div
      ref={layerRef}
      className={backdropClassName}
      onMouseDown={(event) => {
        if (dismissible && event.currentTarget === event.target) onClose();
      }}
      data-modal-variant={variant}
    >
      <section
        {...panelProps}
        ref={panelRef}
        role={role}
        aria-modal={isPopover ? undefined : "true"}
        aria-labelledby={titleId}
        aria-describedby={description ? descriptionId : undefined}
        tabIndex={-1}
        className={panelClassName}
      >
        <span id={titleId} className="sr-only">{title}</span>
        {description && <span id={descriptionId} className="sr-only">{description}</span>}
        {children}
      </section>
    </div>
  );
}
