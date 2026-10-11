import { useRef } from "react";
import { Pin, PinOff } from "lucide-react";

/** Block repeat or IME confirmation keys, including composition-release Enter. */
export function modelPinKeyboardBlocked(event: {
  key: string; isComposing?: boolean; keyCode?: number; repeat?: boolean;
}, elapsedSinceCompositionEnd = Infinity): boolean {
  return Boolean(event.isComposing || event.keyCode === 229 || event.repeat
    || (event.key === "Enter" && elapsedSinceCompositionEnd < 50));
}

/** Toggle a model pin without selecting its surrounding search result. */
export function ModelPinButton({ label, pinned, onToggle, disabled = false }: {
  label: string; pinned: boolean; onToggle: () => void; disabled?: boolean;
}) {
  const composing = useRef(false);
  const compositionEndedAt = useRef(-Infinity);
  const title = `${label}を${pinned ? "ピン留め解除" : "ピン留め"}`;
  const Icon = pinned ? PinOff : Pin;
  return <button
    type="button" aria-pressed={pinned} aria-label={title} title={title} disabled={disabled}
    className="shrink-0 rounded-lg p-2 text-zinc-400 hover:bg-white/10 hover:text-blue-400 aria-pressed:text-blue-400 focus-visible:outline focus-visible:outline-2 focus-visible:outline-blue-400 disabled:opacity-40"
    onPointerDown={(event) => { event.preventDefault(); event.stopPropagation(); }}
    onCompositionStart={() => { composing.current = true; }}
    onCompositionEnd={() => {
      composing.current = false;
      compositionEndedAt.current = performance.now();
    }}
    onKeyDown={(event) => {
      if (event.key !== "Enter" && event.key !== " ") return;
      event.stopPropagation();
      event.preventDefault();
      if (disabled || modelPinKeyboardBlocked({
        key: event.key, isComposing: composing.current || event.nativeEvent.isComposing,
        keyCode: event.nativeEvent.keyCode, repeat: event.repeat,
      }, performance.now() - compositionEndedAt.current)) return;
      onToggle();
    }}
    onKeyUp={(event) => {
      if (event.key !== "Enter" && event.key !== " ") return;
      event.stopPropagation();
      event.preventDefault();
    }}
    onClick={(event) => {
      event.stopPropagation();
      if (!disabled && !composing.current) onToggle();
    }}
  ><Icon size={16} aria-hidden="true" /></button>;
}
