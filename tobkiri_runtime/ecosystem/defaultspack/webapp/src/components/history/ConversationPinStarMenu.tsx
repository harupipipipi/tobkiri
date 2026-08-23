import { Check, Copy, MoreHorizontal, Pin, PinOff, Star, X } from "lucide-react";
import { useCallback, useEffect, useId, useRef, useState } from "react";

import { LayerPortal } from "../../ui/layers/LayerPortal";

type MenuPosition = { left: number; top: number };
type CopyState = "idle" | "copied" | "failed";

const MENU_WIDTH = 176;
const MENU_ESTIMATED_HEIGHT = 120;
const MENU_MARGIN = 8;

export function conversationMenuPosition(
  rect: Pick<DOMRect, "bottom" | "right" | "top">,
  viewport: { width: number; height: number },
): MenuPosition {
  const left = Math.min(
    Math.max(MENU_MARGIN, rect.right - MENU_WIDTH),
    Math.max(MENU_MARGIN, viewport.width - MENU_WIDTH - MENU_MARGIN),
  );
  const below = rect.bottom + 4;
  const top = below + MENU_ESTIMATED_HEIGHT > viewport.height - MENU_MARGIN
    ? Math.max(MENU_MARGIN, rect.top - MENU_ESTIMATED_HEIGHT - 4)
    : below;
  return { left, top };
}

async function copyText(text: string): Promise<void> {
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(text);
    return;
  }

  const textarea = document.createElement("textarea");
  textarea.value = text;
  textarea.readOnly = true;
  textarea.style.position = "fixed";
  textarea.style.left = "-9999px";
  document.body.appendChild(textarea);
  textarea.select();
  try {
    if (!document.execCommand("copy")) throw new Error("Copy failed");
  } finally {
    textarea.remove();
  }
}

export function ConversationPinStarMenu({
  chatId,
  isPinned = false,
  isStarred = false,
  onTogglePinned,
  onToggleStarred,
}: {
  chatId: string;
  isPinned?: boolean;
  isStarred?: boolean;
  onTogglePinned?: () => void;
  onToggleStarred?: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [menuPosition, setMenuPosition] = useState<MenuPosition | null>(null);
  const [copyState, setCopyState] = useState<CopyState>("idle");
  const containerRef = useRef<HTMLDivElement | null>(null);
  const buttonRef = useRef<HTMLButtonElement | null>(null);
  const menuRef = useRef<HTMLDivElement | null>(null);
  const menuId = `conversation-actions-${useId().replace(/:/g, "")}`;

  const updateMenuPosition = useCallback(() => {
    const rect = buttonRef.current?.getBoundingClientRect();
    if (!rect) return;
    setMenuPosition(conversationMenuPosition(rect, {
      width: window.innerWidth,
      height: window.innerHeight,
    }));
  }, []);

  const closeMenu = useCallback((restoreFocus = false) => {
    setOpen(false);
    setCopyState("idle");
    if (restoreFocus) {
      window.requestAnimationFrame(() => buttonRef.current?.focus());
    }
  }, []);

  const openMenu = useCallback(() => {
    updateMenuPosition();
    setOpen(true);
  }, [updateMenuPosition]);

  useEffect(() => {
    if (!open) return;

    updateMenuPosition();
    const closeOnOutsidePointer = (event: PointerEvent) => {
      const target = event.target;
      if (!(target instanceof Node)) return;
      if (containerRef.current?.contains(target) || menuRef.current?.contains(target)) return;
      closeMenu();
    };
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      closeMenu(true);
    };
    document.addEventListener("pointerdown", closeOnOutsidePointer);
    document.addEventListener("keydown", closeOnEscape);
    window.addEventListener("resize", updateMenuPosition);
    window.addEventListener("scroll", updateMenuPosition, true);
    return () => {
      document.removeEventListener("pointerdown", closeOnOutsidePointer);
      document.removeEventListener("keydown", closeOnEscape);
      window.removeEventListener("resize", updateMenuPosition);
      window.removeEventListener("scroll", updateMenuPosition, true);
    };
  }, [closeMenu, open, updateMenuPosition]);

  useEffect(() => {
    if (!open || !menuPosition) return;
    menuRef.current?.querySelector<HTMLButtonElement>('[role="menuitem"]')?.focus();
  }, [open]);

  useEffect(() => {
    if (copyState === "idle") return;
    const timer = window.setTimeout(() => setCopyState("idle"), 1400);
    return () => window.clearTimeout(timer);
  }, [copyState]);

  const handleCopyChatId = async () => {
    try {
      await copyText(chatId);
      setCopyState("copied");
    } catch {
      setCopyState("failed");
    }
  };

  const handleMenuKeyDown = (event: React.KeyboardEvent<HTMLDivElement>) => {
    if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) return;
    const items = Array.from(
      menuRef.current?.querySelectorAll<HTMLButtonElement>('[role="menuitem"]') ?? [],
    );
    if (items.length === 0) return;
    event.preventDefault();
    const currentIndex = items.indexOf(document.activeElement as HTMLButtonElement);
    const nextIndex = event.key === "Home"
      ? 0
      : event.key === "End"
        ? items.length - 1
        : event.key === "ArrowDown"
          ? (currentIndex + 1 + items.length) % items.length
          : (currentIndex - 1 + items.length) % items.length;
    items[nextIndex]?.focus();
  };

  const isolateRowInteraction = (event: React.SyntheticEvent) => {
    event.stopPropagation();
  };

  return (
    <div ref={containerRef} className="relative flex flex-shrink-0 items-center gap-0.5">
      {isPinned && <Pin size={10} className="text-sky-300" aria-hidden="true" />}
      {isStarred && <Star size={10} className="fill-current text-amber-300" aria-hidden="true" />}
      <button
        ref={buttonRef}
        type="button"
        draggable={false}
        aria-label="Conversation actions"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={open ? menuId : undefined}
        data-open={open ? "true" : "false"}
        onPointerDown={isolateRowInteraction}
        onDragStart={(event) => {
          event.preventDefault();
          event.stopPropagation();
        }}
        onDoubleClick={isolateRowInteraction}
        onKeyDown={(event) => {
          if (event.key !== "ArrowDown") return;
          event.preventDefault();
          event.stopPropagation();
          openMenu();
        }}
        onClick={(event) => {
          event.stopPropagation();
          if (open) closeMenu();
          else openMenu();
        }}
        className="pointer-events-none flex h-7 w-7 shrink-0 items-center justify-center rounded text-zinc-600 opacity-0 transition-[opacity,color,background-color,box-shadow] hover:bg-zinc-800 hover:text-zinc-200 focus:pointer-events-auto focus:opacity-100 group-hover/chat:pointer-events-auto group-hover/chat:opacity-100 group-focus-within/chat:pointer-events-auto group-focus-within/chat:opacity-100 data-[open=true]:pointer-events-auto data-[open=true]:opacity-100 [@media(hover:none)]:pointer-events-auto [@media(hover:none)]:opacity-70"
        title="Conversation actions"
      >
        <MoreHorizontal size={14} aria-hidden="true" />
      </button>
      {open && menuPosition && (
        <LayerPortal layer="globalOverlay">
          <div
            ref={menuRef}
            id={menuId}
            role="menu"
            aria-label="Conversation actions"
            className="rumi-popover fixed w-44 overflow-hidden rounded-md border border-zinc-800 bg-zinc-950 py-1 shadow-xl"
            style={{ left: `${menuPosition.left}px`, top: `${menuPosition.top}px` }}
            onPointerDown={isolateRowInteraction}
            onDoubleClick={isolateRowInteraction}
            onClick={isolateRowInteraction}
            onKeyDown={handleMenuKeyDown}
          >
            <button
              type="button"
              role="menuitem"
              onClick={() => void handleCopyChatId()}
              className="flex w-full items-center gap-2 px-2.5 py-1.5 text-left text-[11px] text-zinc-300 hover:bg-zinc-800"
            >
              {copyState === "copied" ? (
                <Check size={12} className="text-emerald-300" aria-hidden="true" />
              ) : copyState === "failed" ? (
                <X size={12} className="text-rose-300" aria-hidden="true" />
              ) : (
                <Copy size={12} aria-hidden="true" />
              )}
              <span aria-live="polite">
                {copyState === "copied"
                  ? "Copied chat ID"
                  : copyState === "failed"
                    ? "Copy failed"
                    : "Copy chat ID"}
              </span>
            </button>
            {onTogglePinned && (
              <button
                type="button"
                role="menuitem"
                onClick={() => {
                  onTogglePinned();
                  closeMenu(true);
                }}
                className="flex w-full items-center gap-2 px-2.5 py-1.5 text-left text-[11px] text-zinc-300 hover:bg-zinc-800"
              >
                {isPinned ? <PinOff size={12} aria-hidden="true" /> : <Pin size={12} aria-hidden="true" />}
                {isPinned ? "Unpin" : "Pin"}
              </button>
            )}
            {onToggleStarred && (
              <button
                type="button"
                role="menuitem"
                onClick={() => {
                  onToggleStarred();
                  closeMenu(true);
                }}
                className="flex w-full items-center gap-2 px-2.5 py-1.5 text-left text-[11px] text-zinc-300 hover:bg-zinc-800"
              >
                <Star
                  size={12}
                  className={isStarred ? "fill-current text-amber-300" : undefined}
                  aria-hidden="true"
                />
                {isStarred ? "Unstar" : "Star"}
              </button>
            )}
          </div>
        </LayerPortal>
      )}
    </div>
  );
}
