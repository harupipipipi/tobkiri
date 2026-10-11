import { useEffect, useRef, useState } from "react";

import { shortcutSpecMatchesEvent } from "../lib/keyboardShortcuts";
import { spotlightShortcutLabel as formatSpotlightShortcutLabel } from "../lib/spotlightNavigation";
import type { SettingsFieldRendererProps } from "./settings/fieldRendererRegistry";
import { shortcutFromKeyEvent } from "./settings/renderers/shortcutRecorderField";
import { SettingsFieldShell } from "./settings/renderers/settingsFieldRendererUtils";

type CaptureKeyEvent = {
  key: string;
  altKey?: boolean;
  ctrlKey?: boolean;
  metaKey?: boolean;
  shiftKey?: boolean;
  isComposing?: boolean;
  keyCode?: number;
  repeat?: boolean;
  getModifierState?: (key: string) => boolean;
};

type ConsumableCaptureEvent = CaptureKeyEvent & {
  preventDefault: () => void;
  stopPropagation: () => void;
  stopImmediatePropagation: () => void;
};

/** Record only keys that round-trip through the application's shortcut matcher. */
export function spotlightShortcutFromEvent(event: CaptureKeyEvent): string | null {
  if (event.isComposing || event.keyCode === 229 || event.repeat
    || event.getModifierState?.("AltGraph")) return null;
  if (!/^(?:[ -~]|Arrow(?:Up|Down|Left|Right)|Enter|Tab|Backspace|Delete|Insert|Home|End|PageUp|PageDown|F(?:[1-9]|1[0-2]))$/u.test(event.key)) return null;
  const next = shortcutFromKeyEvent({
    key: event.key,
    altKey: Boolean(event.altKey),
    ctrlKey: Boolean(event.ctrlKey),
    metaKey: Boolean(event.metaKey),
    shiftKey: Boolean(event.shiftKey),
  });
  return next && shortcutSpecMatchesEvent(next, event, { allowTextInput: true }) ? next : null;
}

/** Consume deliberate recording without letting the key reach global actions. */
export function handleSpotlightShortcutCapture(
  event: ConsumableCaptureEvent,
  capturing: boolean,
  onRecord: (shortcut: string) => void,
): "inactive" | "waiting" | "cancelled" | "recorded" {
  if (!capturing) return "inactive";
  event.stopPropagation();
  event.stopImmediatePropagation();
  // IME candidate keys retain their native behavior without saving a shortcut.
  if (event.isComposing || event.keyCode === 229) return "waiting";
  if (event.key === "Tab" && !event.altKey && !event.ctrlKey && !event.metaKey) {
    return "cancelled";
  }
  event.preventDefault();
  if (event.key === "Escape") return "cancelled";
  const next = spotlightShortcutFromEvent(event);
  if (!next) return "waiting";
  onRecord(next);
  return "recorded";
}

/** The Spotlight field uses the existing Settings change and save pipeline. */
export function SpotlightShortcutRecorder({
  sectionId, field, value, onChange,
  isMac = typeof navigator !== "undefined" && /Mac|iPhone|iPad/.test(navigator.platform),
}: SettingsFieldRendererProps & { isMac?: boolean }) {
  const [capturing, setCapturing] = useState(false);
  const buttonRef = useRef<HTMLButtonElement>(null);
  const fallback = String(field.default ?? "Ctrl+K").trim() || "Ctrl+K";
  const shortcut = String(value ?? fallback).trim() || fallback;
  const label = formatSpotlightShortcutLabel(shortcut, isMac);
  useEffect(() => {
    if (!capturing) return;
    const listener = (event: KeyboardEvent) => {
      const disposition = handleSpotlightShortcutCapture(event, true, (next) => {
        onChange(sectionId, field.id, next);
      });
      if (disposition === "cancelled" || disposition === "recorded") setCapturing(false);
    };
    // Run before document capture (dialog Escape) and bubbling Composer keys.
    window.addEventListener("keydown", listener, true);
    return () => window.removeEventListener("keydown", listener, true);
  }, [capturing, field.id, onChange, sectionId]);

  return <SettingsFieldShell field={field}>
    <div className="flex flex-wrap items-center gap-2" data-spotlight-shortcut-recorder="">
      <button
        ref={buttonRef} type="button" aria-pressed={capturing}
        aria-label={capturing ? `${field.label}: キーを記録中` : `${field.label}: ${label}。クリックして記録`}
        onClick={() => { buttonRef.current?.focus(); setCapturing(true); }}
        onBlur={() => setCapturing(false)}
        className="inline-flex min-h-10 min-w-36 items-center justify-center rounded-lg border border-zinc-800 bg-zinc-900 px-3 py-2 font-mono text-sm text-zinc-200 transition-colors hover:border-zinc-700 focus:border-cyan-400/70 focus:outline-none"
      >{capturing ? "組み合わせを押してください" : label}</button>
      {capturing && <button type="button" onClick={() => setCapturing(false)} className="min-h-9 rounded-md px-2.5 text-xs text-zinc-400 hover:bg-white/[0.045]">キャンセル</button>}
      {shortcut !== fallback && <button type="button" onClick={() => {
        setCapturing(false);
        onChange(sectionId, field.id, fallback);
      }} className="min-h-9 rounded-md px-2.5 text-xs text-zinc-400 hover:bg-white/[0.045]">既定に戻す</button>}
      <span className="text-[11px] text-zinc-500" role="status" aria-live="polite">
        {capturing ? "Ctrl / Cmd / Alt とキー、または F1〜F12。Escでキャンセル" : "クリックしてキーを記録"}
      </span>
    </div>
  </SettingsFieldShell>;
}
