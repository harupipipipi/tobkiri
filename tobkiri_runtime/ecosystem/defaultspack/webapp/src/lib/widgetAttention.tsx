import { useEffect, useState, type ReactNode } from "react";

export const ICON_ATTENTION_TONES = ["info", "danger", "warning", "success", "neutral"] as const;
export const ICON_ATTENTION_EFFECTS = ["none", "pulse"] as const;

export type IconAttentionTone = typeof ICON_ATTENTION_TONES[number];
export type IconAttentionEffect = typeof ICON_ATTENTION_EFFECTS[number];

export type WidgetPresentation = {
  icon_attention?: unknown;
};

export type IconAttentionState = {
  active: true;
  tone: IconAttentionTone;
  effect: IconAttentionEffect;
  accessibleLabel: string;
};

const TONES = new Set<string>(ICON_ATTENTION_TONES);
const EFFECTS = new Set<string>(ICON_ATTENTION_EFFECTS);
const DEFAULT_ACCESSIBLE_LABEL = "Widget needs attention";

const TONE_CLASSES: Record<IconAttentionTone, string> = {
  info: "text-sky-300",
  danger: "text-red-300",
  warning: "text-amber-300",
  success: "text-emerald-300",
  neutral: "text-zinc-300",
};

const CUE_CLASSES: Record<IconAttentionTone, string> = {
  info: "bg-sky-300 ring-sky-200/40",
  danger: "bg-red-300 ring-red-200/40",
  warning: "bg-amber-300 ring-amber-200/40",
  success: "bg-emerald-300 ring-emerald-200/40",
  neutral: "bg-zinc-300 ring-zinc-100/40",
};

function normalizedAccessibleLabel(value: unknown): string {
  const label = typeof value === "string" ? value.trim().replace(/\s+/g, " ") : "";
  return label ? label.slice(0, 160) : DEFAULT_ACCESSIBLE_LABEL;
}

export function normalizeIconAttention(value: unknown): IconAttentionState | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const record = value as Record<string, unknown>;
  if (record.active !== true) return null;
  const tone = typeof record.tone === "string" ? record.tone.trim().toLowerCase() : "";
  const effect = typeof record.effect === "string" ? record.effect.trim().toLowerCase() : "";
  if (!TONES.has(tone) || !EFFECTS.has(effect)) return null;
  return {
    active: true,
    tone: tone as IconAttentionTone,
    effect: effect as IconAttentionEffect,
    accessibleLabel: normalizedAccessibleLabel(record.accessible_label),
  };
}

export function iconAttentionAnimationEnabled(
  attention: IconAttentionState | null,
  environment: { reducedMotion: boolean; visible: boolean },
): boolean {
  return attention?.effect === "pulse"
    && !environment.reducedMotion
    && environment.visible;
}

function browserAnimationEnabled(attention: IconAttentionState | null): boolean {
  if (typeof window === "undefined" || typeof document === "undefined") return false;
  return iconAttentionAnimationEnabled(attention, {
    reducedMotion: window.matchMedia("(prefers-reduced-motion: reduce)").matches,
    visible: document.visibilityState === "visible",
  });
}

function useIconAttentionAnimation(attention: IconAttentionState | null): boolean {
  const [enabled, setEnabled] = useState(() => browserAnimationEnabled(attention));
  useEffect(() => {
    if (typeof window === "undefined" || typeof document === "undefined") return undefined;
    const motionQuery = window.matchMedia("(prefers-reduced-motion: reduce)");
    const refresh = () => setEnabled(browserAnimationEnabled(attention));
    refresh();
    motionQuery.addEventListener("change", refresh);
    document.addEventListener("visibilitychange", refresh);
    return () => {
      motionQuery.removeEventListener("change", refresh);
      document.removeEventListener("visibilitychange", refresh);
    };
  }, [attention?.effect]);
  return enabled;
}

export function WidgetAttentionIcon({
  attention: value,
  children,
  motionEnabled,
  widgetId,
}: {
  attention: unknown;
  children?: ReactNode;
  motionEnabled?: boolean;
  widgetId?: string;
}) {
  const attention = normalizeIconAttention(value);
  const browserMotionEnabled = useIconAttentionAnimation(attention);
  if (!attention) return children;
  const animate = motionEnabled ?? browserMotionEnabled;
  return (
    <span
      className={`relative inline-flex shrink-0 items-center justify-center ${TONE_CLASSES[attention.tone]} ${animate ? "rumi-widget-attention-pulse" : ""}`}
      role="img"
      aria-label={attention.accessibleLabel}
      data-widget-icon-attention="active"
      data-attention-tone={attention.tone}
      data-attention-effect={attention.effect}
      data-widget-id={widgetId || undefined}
    >
      <span aria-hidden="true" className="inline-flex items-center justify-center">
        {children}
      </span>
      <span
        aria-hidden="true"
        data-widget-attention-cue="dot"
        className={`pointer-events-none absolute -right-0.5 -top-0.5 h-1.5 w-1.5 rounded-full ring-1 ${CUE_CLASSES[attention.tone]}`}
      />
    </span>
  );
}
