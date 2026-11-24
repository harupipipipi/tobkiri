const KEYBOARD_FOCUS_CLASS = "rumi-keyboard-focus";

/**
 * Show focus rings only after keyboard Tab navigation.
 *
 * Text controls match `:focus-visible` even when they are focused by a
 * pointer, so CSS alone cannot distinguish the interaction the product wants.
 * Pointer input clears the keyboard modality before focus is applied.
 */
export function installKeyboardOnlyFocusRings(doc: Document = document): () => void {
  const root = doc.documentElement;
  const enableForTab = (event: KeyboardEvent) => {
    if (event.key === "Tab") root.classList.add(KEYBOARD_FOCUS_CLASS);
  };
  const disableForPointer = () => root.classList.remove(KEYBOARD_FOCUS_CLASS);

  doc.addEventListener("keydown", enableForTab, true);
  doc.addEventListener("pointerdown", disableForPointer, true);

  return () => {
    doc.removeEventListener("keydown", enableForTab, true);
    doc.removeEventListener("pointerdown", disableForPointer, true);
    root.classList.remove(KEYBOARD_FOCUS_CLASS);
  };
}
