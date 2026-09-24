import { MoreHorizontal, Pin, PinOff, Star } from "lucide-react";
import { useEffect, useId, useRef, useState } from "react";
import { LayerPortal } from "../../ui/layers/LayerPortal";

export function ConversationPinStarMenu({
  isPinned = false,
  isStarred = false,
  onTogglePinned,
  onToggleStarred,
}: {
  isPinned?: boolean;
  isStarred?: boolean;
  onTogglePinned?: () => void;
  onToggleStarred?: () => void;
}) {
  const [position, setPosition] = useState<{ left: number; top: number } | null>(null);
  const ref = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const menuId = useId();
  const open = position !== null;
  const close = () => {
    setPosition(null);
    triggerRef.current?.focus();
  };

  useEffect(() => {
    if (!open) return;
    menuRef.current?.querySelector<HTMLButtonElement>("button")?.focus();
    const dismiss = (event: PointerEvent) => {
      const target = event.target;
      if (target instanceof Node && (ref.current?.contains(target) || menuRef.current?.contains(target))) return;
      setPosition(null);
    };
    const dismissForLayout = (event: Event) => {
      if (event.target instanceof Node && menuRef.current?.contains(event.target)) return;
      setPosition(null);
    };
    document.addEventListener("pointerdown", dismiss);
    window.addEventListener("resize", dismissForLayout);
    window.addEventListener("scroll", dismissForLayout, true);
    return () => {
      document.removeEventListener("pointerdown", dismiss);
      window.removeEventListener("resize", dismissForLayout);
      window.removeEventListener("scroll", dismissForLayout, true);
    };
  }, [open]);

  return (
    <div
      ref={ref}
      className="relative flex shrink-0 items-center gap-0.5"
      onPointerDown={(event) => event.stopPropagation()}
      onClick={(event) => event.stopPropagation()}
      onDoubleClick={(event) => event.stopPropagation()}
      onKeyDown={(event) => event.stopPropagation()}
    >
      {isPinned && <Pin size={10} className="text-zinc-300" />}
      {isStarred && <Star size={10} className="fill-current text-zinc-300" />}
      {(onTogglePinned || onToggleStarred) && (
        <button
          ref={triggerRef}
          type="button"
          aria-label="Conversation actions"
          aria-haspopup="menu"
          aria-expanded={open}
          aria-controls={open ? menuId : undefined}
          onClick={(event) => {
            if (open) { close(); return; }
            const rect = event.currentTarget.getBoundingClientRect();
            const height = 8 + (Number(Boolean(onTogglePinned)) + Number(Boolean(onToggleStarred))) * 36;
            setPosition({
              left: Math.max(8, Math.min(rect.right - 160, window.innerWidth - 168)),
              top: rect.bottom + height + 12 <= window.innerHeight ? rect.bottom + 4 : Math.max(8, rect.top - height - 4),
            });
          }}
          className="flex h-6 w-6 items-center justify-center rounded text-zinc-500 hover:bg-zinc-700 hover:text-zinc-100 focus-visible:bg-zinc-700 focus-visible:text-zinc-100"
          title="Conversation actions"
        >
          <MoreHorizontal size={15} />
        </button>
      )}
      {position && (
        <LayerPortal layer="globalOverlay">
          <div
            ref={menuRef}
            id={menuId}
            role="menu"
            aria-label="Conversation actions"
            className="fixed w-40 overflow-hidden rounded-lg border border-zinc-700 bg-zinc-900 p-1 shadow-xl"
            style={position}
            onKeyDown={(event) => {
              event.stopPropagation();
              if (event.key === "Escape") { event.preventDefault(); close(); }
              if (event.key === "Tab") setPosition(null);
              if (["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) {
                event.preventDefault();
                const items = Array.from(menuRef.current?.querySelectorAll<HTMLButtonElement>("button") ?? []);
                const index = items.indexOf(document.activeElement as HTMLButtonElement);
                const next = event.key === "Home" ? 0 : event.key === "End" ? items.length - 1 : (index + (event.key === "ArrowDown" ? 1 : -1) + items.length) % items.length;
                items[next]?.focus();
              }
            }}
          >
            {onTogglePinned && (
              <button type="button" role="menuitem" onClick={() => { onTogglePinned(); close(); }} className="flex h-9 w-full items-center gap-2 rounded px-2.5 text-left text-xs text-zinc-200 hover:bg-zinc-800 focus:bg-zinc-800 focus:outline-none">
                {isPinned ? <PinOff size={14} /> : <Pin size={14} />}
                {isPinned ? "Unpin" : "Pin"}
              </button>
            )}
            {onToggleStarred && (
              <button type="button" role="menuitem" onClick={() => { onToggleStarred(); close(); }} className="flex h-9 w-full items-center gap-2 rounded px-2.5 text-left text-xs text-zinc-200 hover:bg-zinc-800 focus:bg-zinc-800 focus:outline-none">
                <Star size={14} className={isStarred ? "fill-current" : undefined} />
                {isStarred ? "Unstar" : "Star"}
              </button>
            )}
          </div>
        </LayerPortal>
      )}
    </div>
  );
}
