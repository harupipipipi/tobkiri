import { Ban, Check, ChevronDown, ShieldCheck, SlidersHorizontal, Sparkles } from "lucide-react";
import { useCallback, useEffect, useLayoutEffect, useRef, useState, type CSSProperties } from "react";
import { createPortal } from "react-dom";

import type { ToolSelectionMode } from "./types";

const MENU_WIDTH = 360;
const MENU_HEIGHT_ESTIMATE = 244;
const MENU_GAP = 10;
const MENU_MARGIN = 8;

const MODE_OPTIONS: Array<{
  mode: ToolSelectionMode;
  label: string;
  shortLabel: string;
  title: string;
  description: string;
  icon: typeof Sparkles;
}> = [
  { mode: "auto", label: "機能 自動", shortLabel: "自動", title: "自動で選ぶ", description: "例：『Webで天気を調べて』→検索を自動で選びます。", icon: Sparkles },
  { mode: "review", label: "機能 確認", shortLabel: "確認", title: "使う前に確認", description: "例：検索する前に、使うツールの候補を確認します。", icon: ShieldCheck },
  { mode: "manual", label: "機能 手動", shortLabel: "手動", title: "自分で選ぶ", description: "例：@Web Searchを確定すると、検索だけを使います。", icon: SlidersHorizontal },
  { mode: "none", label: "機能 なし", shortLabel: "なし", title: "機能を使わない", description: "例：文章の言い換えを、検索やファイル操作なしで答えます。", icon: Ban },
];

export function ToolModeControl({
  mode,
  manualCount = 0,
  disabled = false,
  surfaceClassName,
  tabIndex,
  onModeChange,
  onOpenPicker,
  availableModes,
}: {
  mode: ToolSelectionMode;
  manualCount?: number;
  disabled?: boolean;
  surfaceClassName: string;
  tabIndex?: number;
  onModeChange: (mode: ToolSelectionMode) => void;
  onOpenPicker?: () => void;
  availableModes?: readonly ToolSelectionMode[];
}) {
  const [open, setOpen] = useState(false);
  const [menuStyle, setMenuStyle] = useState<CSSProperties | null>(null);
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const current = MODE_OPTIONS.find((option) => option.mode === mode) ?? MODE_OPTIONS[0];
  const Icon = current.icon;

  const closeMenu = useCallback(() => {
    setOpen(false);
    triggerRef.current?.focus();
  }, []);

  const updateMenuStyle = useCallback(() => {
    if (typeof window === "undefined" || !triggerRef.current) return;
    const rect = triggerRef.current.getBoundingClientRect();
    if (window.innerWidth <= 760) {
      setMenuStyle({
        left: MENU_MARGIN,
        right: MENU_MARGIN,
        bottom: MENU_MARGIN,
        maxHeight: "72vh",
      });
      return;
    }

    const menuWidth = Math.min(MENU_WIDTH, window.innerWidth - MENU_MARGIN * 2);
    const minLeft = MENU_MARGIN;
    const maxLeft = window.innerWidth - menuWidth - MENU_MARGIN;
    const preferredLeft = rect.left + rect.width / 2 - menuWidth / 2;
    const left = Math.min(Math.max(preferredLeft, minLeft), maxLeft);
    const spaceAbove = rect.top - MENU_MARGIN;
    const spaceBelow = window.innerHeight - rect.bottom - MENU_MARGIN;
    const placeBelow = spaceBelow >= MENU_HEIGHT_ESTIMATE || spaceBelow > spaceAbove;
    const preferredTop = placeBelow
      ? rect.bottom + MENU_GAP
      : rect.top - MENU_HEIGHT_ESTIMATE - MENU_GAP;
    const maxTop = window.innerHeight - MENU_HEIGHT_ESTIMATE - MENU_MARGIN;
    const top = Math.min(Math.max(preferredTop, MENU_MARGIN), Math.max(MENU_MARGIN, maxTop));
    setMenuStyle({
      left,
      top,
      width: menuWidth,
      maxHeight: Math.min(MENU_HEIGHT_ESTIMATE, window.innerHeight - MENU_MARGIN * 2),
    });
  }, []);

  useLayoutEffect(() => {
    if (!open) {
      setMenuStyle(null);
      return;
    }
    updateMenuStyle();
    if (typeof window === "undefined") return;
    window.addEventListener("resize", updateMenuStyle);
    window.addEventListener("scroll", updateMenuStyle, true);
    return () => {
      window.removeEventListener("resize", updateMenuStyle);
      window.removeEventListener("scroll", updateMenuStyle, true);
    };
  }, [open, updateMenuStyle]);

  useEffect(() => {
    if (!open) return;
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        closeMenu();
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [closeMenu, open]);

  const menu = open && typeof document !== "undefined" ? createPortal(
    <>
      <button
        type="button"
        aria-label="機能メニューを閉じる"
        className="fixed inset-0 rumi-layer-local-popover cursor-default bg-transparent"
        onClick={closeMenu}
      />
      <div
        role="menu"
        aria-label="機能の使い方"
        style={menuStyle ?? undefined}
        className="fixed rumi-layer-command-palette overflow-y-auto rounded-[1.35rem] border border-zinc-700/70 bg-[#2b2b2b] p-2 shadow-2xl shadow-black/40 max-[760px]:rounded-[1.6rem]"
      >
        {MODE_OPTIONS.filter((option) => !availableModes || availableModes.includes(option.mode)).map((option) => {
          const OptionIcon = option.icon;
          const selected = option.mode === mode;
          return (
            <button
              key={option.mode}
              type="button"
              role="menuitemradio"
              aria-checked={selected}
              tabIndex={tabIndex}
              onClick={() => {
                onModeChange(option.mode);
                setOpen(false);
                if (option.mode === "manual" && onOpenPicker) {
                  window.setTimeout(onOpenPicker, 0);
                }
              }}
              className={`flex min-h-[54px] w-full items-center gap-3 rounded-[1rem] px-4 py-2.5 text-left transition-colors ${
                selected ? "bg-zinc-700/55 text-zinc-50" : "text-zinc-200 hover:bg-zinc-700/35"
              }`}
            >
              <OptionIcon size={18} className="flex-shrink-0 text-zinc-300" />
              <span className="min-w-0 flex-1">
                <span className="block text-[14px] font-medium leading-5">{option.title}</span>
                <span className="block text-[12px] leading-4 text-zinc-400">{option.description}</span>
              </span>
              {selected && <Check size={18} className="flex-shrink-0 text-zinc-200" />}
            </button>
          );
        })}
      </div>
    </>,
    document.body,
  ) : null;

  return (
    <div className="relative flex min-w-0">
      <button
        ref={triggerRef}
        type="button"
        tabIndex={tabIndex}
        disabled={disabled}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label="機能の使い方"
        title="機能の使い方"
        onClick={() => setOpen((value) => !value)}
        className={`${surfaceClassName} min-w-[92px] max-w-[144px] gap-1.5 text-zinc-300 transition-colors hover:border-zinc-600 hover:text-zinc-100 disabled:opacity-50 max-[760px]:min-w-[72px]`}
      >
        <Icon size={15} className="flex-shrink-0" />
        <span className="min-w-0 truncate text-[12px] font-medium max-[760px]:hidden">
          {current.label}
        </span>
        <span className="hidden min-w-0 truncate text-[12px] font-medium max-[760px]:inline">
          {current.shortLabel}
        </span>
        {mode === "manual" && manualCount > 0 && (
          <span className="ml-0.5 rounded-full bg-zinc-700 px-1.5 text-[10px] text-zinc-100">{manualCount}</span>
        )}
        <ChevronDown size={12} className={`flex-shrink-0 text-zinc-500 transition-transform ${open ? "rotate-180" : ""}`} />
      </button>
      {menu}
    </div>
  );
}
