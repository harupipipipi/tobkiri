type ShortcutSpec = {
  alt: boolean;
  ctrl: boolean;
  key: string;
  meta: boolean;
  shift: boolean;
};

type KeyboardEventLike = {
  altKey?: boolean;
  ctrlKey?: boolean;
  key?: string;
  metaKey?: boolean;
  shiftKey?: boolean;
  target?: EventTarget | null;
  isComposing?: boolean;
  defaultPrevented?: boolean;
  repeat?: boolean;
};

export type WorkspaceTabShortcutAction = "create_chat" | "close_active" | "restore_last_closed";

const MODIFIER_ALIASES: Record<string, keyof Omit<ShortcutSpec, "key">> = {
  alt: "alt",
  option: "alt",
  ctrl: "ctrl",
  control: "ctrl",
  mod: "ctrl",
  cmd: "meta",
  command: "meta",
  meta: "meta",
  super: "meta",
  win: "meta",
  windows: "meta",
  shift: "shift",
};

const DISPLAY_KEYS: Record<string, string> = {
  " ": "Space",
  arrowdown: "ArrowDown",
  arrowleft: "ArrowLeft",
  arrowright: "ArrowRight",
  arrowup: "ArrowUp",
  backspace: "Backspace",
  delete: "Delete",
  enter: "Enter",
  escape: "Esc",
  esc: "Esc",
  space: "Space",
  tab: "Tab",
};

const NON_TEXT_INPUT_TYPES = new Set([
  "button",
  "checkbox",
  "color",
  "file",
  "hidden",
  "image",
  "radio",
  "range",
  "reset",
  "submit",
]);

export function normalizeShortcutSpec(value: unknown): ShortcutSpec | null {
  const text = String(value ?? "").trim();
  if (!text || ["off", "none", "disabled", "false", "0"].includes(text.toLowerCase())) {
    return null;
  }
  const spec: ShortcutSpec = { alt: false, ctrl: false, key: "", meta: false, shift: false };
  const parts = text.split("+").map((part) => part.trim()).filter(Boolean);
  let keyCount = 0;
  for (const part of parts) {
    const normalized = part.toLowerCase();
    const modifier = MODIFIER_ALIASES[normalized];
    if (modifier) {
      spec[modifier] = true;
      continue;
    }
    keyCount += 1;
    if (keyCount > 1) return null;
    spec.key = normalizeShortcutKey(part);
  }
  if (!spec.key || isModifierKey(spec.key)) return null;
  if (!spec.ctrl && !spec.alt && !spec.meta && !spec.shift) return null;
  return spec;
}

export function shortcutLabel(value: unknown): string {
  const spec = normalizeShortcutSpec(value);
  if (!spec) return "Off";
  return [
    spec.ctrl ? "Ctrl" : "",
    spec.meta ? "Win" : "",
    spec.alt ? "Alt" : "",
    spec.shift ? "Shift" : "",
    displayShortcutKey(spec.key),
  ].filter(Boolean).join("+");
}

export function shortcutSpecMatchesEvent(
  value: unknown,
  event: KeyboardEventLike,
  options: { allowTextInput?: boolean } = {},
): boolean {
  const spec = normalizeShortcutSpec(value);
  if (!spec || event.isComposing) return false;
  if (!options.allowTextInput && isTextInputTarget(event.target ?? null)) return false;
  if (Boolean(event.ctrlKey) !== spec.ctrl) return false;
  if (Boolean(event.altKey) !== spec.alt) return false;
  if (Boolean(event.shiftKey) !== spec.shift) return false;
  if (Boolean(event.metaKey) !== spec.meta) return false;
  const key = normalizeShortcutKey(event.key ?? "");
  return key === spec.key;
}

/** Resolve browser-style workspace-tab shortcuts without excluding text inputs. */
export function workspaceTabShortcutAction(
  event: KeyboardEventLike,
): WorkspaceTabShortcutAction | null {
  if (event.defaultPrevented || event.repeat || event.isComposing) return null;
  const matches = (shortcut: string) => shortcutSpecMatchesEvent(
    shortcut,
    event,
    { allowTextInput: true },
  );
  if (matches("Ctrl+Shift+T") || matches("Cmd+Shift+T")) return "restore_last_closed";
  if (matches("Ctrl+T") || matches("Cmd+T")) return "create_chat";
  if (matches("Ctrl+W") || matches("Cmd+W")) return "close_active";
  return null;
}

function normalizeShortcutKey(value: string): string {
  const trimmed = String(value || "").trim();
  if (!trimmed) return "";
  const lower = trimmed.toLowerCase();
  if (lower === "esc") return "escape";
  if (lower === "spacebar") return " ";
  if (lower === "space") return " ";
  return lower;
}

function displayShortcutKey(value: string): string {
  const display = DISPLAY_KEYS[value] ?? DISPLAY_KEYS[value.toLowerCase()];
  if (display) return display;
  if (/^f\d{1,2}$/.test(value.toLowerCase())) return value.toUpperCase();
  return value.length === 1 ? value.toUpperCase() : value;
}

function isModifierKey(value: string): boolean {
  return Boolean(MODIFIER_ALIASES[value.toLowerCase()]);
}

function isTextInputTarget(target: EventTarget | null): boolean {
  const element = target as {
    getAttribute?: (name: string) => string | null;
    isContentEditable?: boolean;
    tagName?: string;
    type?: string;
  } | null;
  if (!element) return false;
  if (element.isContentEditable) return true;
  const role = element.getAttribute?.("role")?.toLowerCase();
  if (role === "textbox" || role === "searchbox") return true;
  const tagName = String(element.tagName || "").toLowerCase();
  if (tagName === "textarea" || tagName === "select") return true;
  if (tagName !== "input") return false;
  return !NON_TEXT_INPUT_TYPES.has(String(element.type || "text").toLowerCase());
}
