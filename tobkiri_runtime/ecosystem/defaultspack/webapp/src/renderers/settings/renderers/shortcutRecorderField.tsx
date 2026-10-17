import { useState, type KeyboardEvent } from "react";

import type { SettingsFieldRendererProps } from "../fieldRendererRegistry";
import { SettingsFieldShell } from "./settingsFieldRendererUtils";

type ShortcutKeyEvent = Pick<KeyboardEvent<HTMLButtonElement>, "key" | "altKey" | "ctrlKey" | "metaKey" | "shiftKey">;

const modifierKeys = new Set(["Alt", "AltGraph", "Control", "Meta", "Shift"]);

function shortcutKeyLabel(key: string): string {
  if (key === " " || key === "Space") return "Space";
  if (key === "Escape") return "Esc";
  if (key === "ArrowUp") return "↑";
  if (key === "ArrowDown") return "↓";
  if (key === "ArrowLeft") return "←";
  if (key === "ArrowRight") return "→";
  return key.length === 1 ? key.toUpperCase() : key;
}

/** Convert one non-modifier browser key event into a saved shortcut string. */
export function shortcutFromKeyEvent(event: ShortcutKeyEvent): string | null {
  if (modifierKeys.has(event.key)) return null;
  const modifiers = [
    event.metaKey ? "Cmd" : "",
    event.ctrlKey ? "Ctrl" : "",
    event.altKey ? "Alt" : "",
    event.shiftKey ? "Shift" : "",
  ].filter(Boolean);
  const isFunctionKey = /^F(?:[1-9]|1[0-2])$/.test(event.key);
  if (modifiers.length === 0 && !isFunctionKey) return null;
  // Keep this token in the form accepted by keyboardShortcuts.ts. In
  // particular, its matcher expects ArrowUp rather than the displayed ↑.
  const key = event.key === " "
    ? "Space"
    : event.key.length === 1 ? event.key.toUpperCase() : event.key;
  return [...modifiers, key].join("+");
}

export function BuiltinShortcutRecorderRenderer({ sectionId, field, value, onChange }: SettingsFieldRendererProps) {
  const [capturing, setCapturing] = useState(false);
  const shortcut = String(value ?? field.default ?? "").trim();
  const fallback = String(field.default ?? "Ctrl+K").trim() || "Ctrl+K";

  const handleKeyDown = (event: KeyboardEvent<HTMLButtonElement>) => {
    if (!capturing) return;
    event.preventDefault();
    event.stopPropagation();
    if (event.key === "Escape") {
      setCapturing(false);
      return;
    }
    const nextShortcut = shortcutFromKeyEvent(event);
    if (!nextShortcut) return;
    onChange(sectionId, field.id, nextShortcut);
    setCapturing(false);
  };

  return (
    <SettingsFieldShell field={field}>
      <div className="flex flex-wrap items-center gap-2">
        <button
          type="button"
          aria-label={capturing ? "キーを入力" : `${field.label}: ${shortcut || fallback}`}
          aria-pressed={capturing}
          onClick={() => setCapturing(true)}
          onKeyDown={handleKeyDown}
          className="inline-flex min-h-10 min-w-36 items-center justify-center rounded-lg border border-zinc-800 bg-zinc-900 px-3 py-2 font-mono text-sm text-zinc-200 transition-colors hover:border-zinc-700 focus:border-cyan-400/70 focus:outline-none"
        >
          {capturing ? "組み合わせを押してください" : shortcut || fallback}
        </button>
        {shortcut && shortcut !== fallback ? (
          <button
            type="button"
            onClick={() => onChange(sectionId, field.id, fallback)}
            className="min-h-9 rounded-md px-2.5 text-xs text-zinc-500 transition-colors hover:bg-white/[0.045] hover:text-zinc-200"
          >
            既定に戻す
          </button>
        ) : null}
        <span className="text-[11px] text-zinc-500" aria-live="polite">
          {capturing ? "Ctrl / Cmd / Alt とキー、または F1〜F12。Escでキャンセル" : "クリックしてキーを記録"}
        </span>
      </div>
    </SettingsFieldShell>
  );
}
