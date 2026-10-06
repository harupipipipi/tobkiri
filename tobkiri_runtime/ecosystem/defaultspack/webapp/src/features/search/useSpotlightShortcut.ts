import { useEffect, useRef } from "react";
import { spotlightShortcutMatchesEvent } from "../../lib/spotlightNavigation";

/** Open global search without stealing keys from another modal or an IME session. */
export function useSpotlightShortcut({ shortcut, enabled, allowTextInput, isMac, blocked, onOpen }: {
  shortcut: string; enabled: boolean; allowTextInput: boolean; isMac: boolean;
  blocked: boolean; onOpen: () => void;
}) {
  const onOpenRef = useRef(onOpen);
  onOpenRef.current = onOpen;
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (!enabled || blocked) return;
      const otherDialog = Array.from(document.querySelectorAll<HTMLElement>('[role="dialog"],[role="alertdialog"],dialog[open]'))
        .some((dialog) => !dialog.closest(".rumi-spotlight-panel") && dialog.getClientRects().length > 0);
      const chord = event.repeat ? {
        key: event.key, ctrlKey: event.ctrlKey, metaKey: event.metaKey,
        altKey: event.altKey, shiftKey: event.shiftKey, target: event.target,
        isComposing: event.isComposing, keyCode: event.keyCode,
        defaultPrevented: event.defaultPrevented, repeat: false,
      } : event;
      // The input policy controls opening from another input. Once Spotlight is
      // open, its own chord must still be consumed rather than reach the browser.
      const insideSpotlight = event.target instanceof Element
        && Boolean(event.target.closest(".rumi-spotlight-panel"));
      if (otherDialog || !spotlightShortcutMatchesEvent(shortcut, chord, {
        allowTextInput: allowTextInput || insideSpotlight, isMac,
      })) return;
      event.preventDefault();
      if (!event.repeat) onOpenRef.current();
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [shortcut, enabled, allowTextInput, isMac, blocked]);
}
