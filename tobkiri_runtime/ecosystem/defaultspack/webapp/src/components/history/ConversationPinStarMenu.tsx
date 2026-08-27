import { Check, MoreHorizontal, Pin, PinOff, Star } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { t, type LocaleSetting } from "../../lib/i18n";

type ConversationCategory = "chat" | "coding" | "research";

export function ConversationPinStarMenu({
  isPinned = false,
  isStarred = false,
  onTogglePinned,
  onToggleStarred,
  category = "chat",
  locale = "en",
  onCategoryChange,
}: {
  isPinned?: boolean;
  isStarred?: boolean;
  onTogglePinned?: () => void;
  onToggleStarred?: () => void;
  category?: ConversationCategory;
  locale?: LocaleSetting;
  onCategoryChange?: (category: ConversationCategory) => void;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!open) return;
    const close = (event: PointerEvent) => {
      const target = event.target;
      if (target instanceof Node && ref.current?.contains(target)) return;
      setOpen(false);
    };
    document.addEventListener("pointerdown", close);
    return () => document.removeEventListener("pointerdown", close);
  }, [open]);

  return (
    <div ref={ref} className="relative flex flex-shrink-0 items-center gap-0.5">
      {isPinned && <Pin size={10} className="text-sky-300" />}
      {isStarred && <Star size={10} className="fill-current text-amber-300" />}
      {(onTogglePinned || onToggleStarred || onCategoryChange) && (
        <button
          type="button"
          onClick={(event) => {
            event.stopPropagation();
            setOpen((value) => !value);
          }}
          className="hidden h-5 w-5 items-center justify-center rounded text-zinc-600 hover:bg-zinc-800 hover:text-zinc-200 group-hover/chat:flex"
          title="Conversation actions"
        >
          <MoreHorizontal size={13} />
        </button>
      )}
      {open && (
        <div
          className="absolute right-0 top-full rumi-layer-global-overlay mt-1 w-40 overflow-hidden rounded-md border border-zinc-800 bg-zinc-950 shadow-xl"
          onClick={(event) => event.stopPropagation()}
        >
          {onTogglePinned && (
            <button
              type="button"
              onClick={() => {
                onTogglePinned();
                setOpen(false);
              }}
              className="flex w-full items-center gap-2 px-2.5 py-1.5 text-left text-[11px] text-zinc-300 hover:bg-zinc-800"
            >
              {isPinned ? <PinOff size={12} /> : <Pin size={12} />}
              {isPinned ? "Unpin" : "Pin"}
            </button>
          )}
          {onToggleStarred && (
            <button
              type="button"
              onClick={() => {
                onToggleStarred();
                setOpen(false);
              }}
              className="flex w-full items-center gap-2 px-2.5 py-1.5 text-left text-[11px] text-zinc-300 hover:bg-zinc-800"
            >
              <Star size={12} className={isStarred ? "fill-current text-amber-300" : undefined} />
              {isStarred ? "Unstar" : "Star"}
            </button>
          )}
          {onCategoryChange && (
            <div className="border-t border-zinc-800 px-1.5 py-1.5">
              <p className="px-1 pb-1 text-[9px] font-semibold uppercase tracking-wide text-zinc-600">
                {t(locale, "history.category.label")}
              </p>
              {(["chat", "coding", "research"] as const).map((option) => (
                <button
                  key={option}
                  type="button"
                  onClick={() => {
                    onCategoryChange(option);
                    setOpen(false);
                  }}
                  className="flex w-full items-center justify-between rounded px-1 py-1 text-left text-[11px] text-zinc-300 hover:bg-zinc-800"
                  aria-pressed={category === option}
                >
                  {t(locale, `history.category.${option}`)}
                  {category === option && <Check size={11} className="text-emerald-300" />}
                </button>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
