import { useEffect, useState, type RefObject } from "react";
import {
  CircleAlert,
  CircleCheck,
  CircleX,
  Info,
  X,
  type LucideIcon,
} from "lucide-react";
import { AnimatePresence, motion, useReducedMotion } from "motion/react";

import { ErrorCopyAction } from "./ErrorNotice";
import { LayerPortal } from "../ui/layers/LayerPortal";

export type TransientAlertTone = "success" | "info" | "warning" | "error";
export type TransientAlertPlacement = "viewport-bottom" | "above-composer";

export type TransientAlertItem = {
  id: string;
  message: string;
  tone?: TransientAlertTone;
  durationMs?: number;
};

export const ALERT_AUTO_DISMISS_MS = 3600;
export const ALERT_ANCHOR_GAP_PX = 12;

export function alertPlacementForComposerPosition(
  position: "center" | "bottom" | "inline" | undefined,
): TransientAlertPlacement {
  return position === "bottom" ? "above-composer" : "viewport-bottom";
}

type AlertPresentation = {
  Icon: LucideIcon;
  accentClassName: string;
  iconClassName: string;
};

export function alertPresentation(tone: TransientAlertTone): AlertPresentation {
  switch (tone) {
    case "info":
      return {
        Icon: Info,
        accentClassName: "bg-sky-400",
        iconClassName: "bg-sky-400/15 text-sky-300",
      };
    case "warning":
      return {
        Icon: CircleAlert,
        accentClassName: "bg-amber-400",
        iconClassName: "bg-amber-400/15 text-amber-300",
      };
    case "error":
      return {
        Icon: CircleX,
        accentClassName: "bg-rose-400",
        iconClassName: "bg-rose-400/15 text-rose-300",
      };
    case "success":
    default:
      return {
        Icon: CircleCheck,
        accentClassName: "bg-emerald-400",
        iconClassName: "bg-emerald-400/15 text-emerald-300",
      };
  }
}

export function alertSemantics(tone: TransientAlertTone): {
  canCopy: boolean;
  live: "assertive" | "polite";
  role: "alert" | "status";
} {
  if (tone === "error") {
    return { canCopy: true, live: "assertive", role: "alert" };
  }
  if (tone === "warning") {
    return { canCopy: true, live: "polite", role: "status" };
  }
  return { canCopy: false, live: "polite", role: "status" };
}

export function TransientAlert({
  alert,
  onDismiss,
  placement = "viewport-bottom",
  anchorRef,
}: {
  alert: TransientAlertItem | null;
  onDismiss: () => void;
  placement?: TransientAlertPlacement;
  anchorRef?: RefObject<HTMLElement | null>;
}) {
  const prefersReducedMotion = useReducedMotion();
  const [anchorBottom, setAnchorBottom] = useState<number | null>(null);

  useEffect(() => {
    if (!alert) return undefined;
    const timer = window.setTimeout(
      onDismiss,
      Math.max(0, alert.durationMs ?? ALERT_AUTO_DISMISS_MS),
    );
    return () => window.clearTimeout(timer);
  }, [alert, onDismiss]);

  useEffect(() => {
    if (!alert || placement !== "above-composer") {
      setAnchorBottom(null);
      return undefined;
    }
    const anchor = anchorRef?.current;
    if (!anchor) {
      setAnchorBottom(null);
      return undefined;
    }
    const updateAnchorBottom = () => {
      const rect = anchor.getBoundingClientRect();
      setAnchorBottom(Math.max(20, window.innerHeight - rect.top + ALERT_ANCHOR_GAP_PX));
    };
    updateAnchorBottom();
    const observer = new ResizeObserver(updateAnchorBottom);
    observer.observe(anchor);
    window.addEventListener("resize", updateAnchorBottom);
    window.addEventListener("scroll", updateAnchorBottom, true);
    return () => {
      observer.disconnect();
      window.removeEventListener("resize", updateAnchorBottom);
      window.removeEventListener("scroll", updateAnchorBottom, true);
    };
  }, [alert, anchorRef, placement]);

  const tone = alert?.tone ?? "success";
  const presentation = alertPresentation(tone);
  const AlertIcon = presentation.Icon;
  const semantics = alertSemantics(tone);

  return (
    <LayerPortal layer="toast">
      <div
        className={`pointer-events-none fixed inset-x-0 flex justify-center px-4 ${
          placement === "viewport-bottom" ? "bottom-5" : ""
        }`}
        data-alert-placement={placement}
        style={placement === "above-composer" ? { bottom: anchorBottom ?? 20 } : undefined}
      >
        <AnimatePresence initial={false}>
          {alert && (
            <motion.div
              key={alert.id}
              role={semantics.role}
              aria-live={semantics.live}
              data-testid="transient-alert"
              className="pointer-events-auto relative flex max-w-[min(520px,calc(100vw-32px))] items-center gap-2.5 overflow-hidden rounded-xl border border-white/[0.09] bg-zinc-950/95 px-3.5 py-2.5 text-sm text-zinc-100 shadow-2xl shadow-black/45 backdrop-blur-xl"
              initial={prefersReducedMotion ? { opacity: 0 } : { opacity: 0, y: 18, scale: 0.98 }}
              animate={{ opacity: 1, y: 0, scale: 1 }}
              exit={prefersReducedMotion ? { opacity: 0 } : { opacity: 0, y: 32, scale: 0.98 }}
              transition={{
                duration: prefersReducedMotion ? 0.12 : 0.24,
                ease: [0.4, 0, 1, 1],
              }}
            >
              <span className={`absolute inset-y-0 left-0 w-0.5 ${presentation.accentClassName}`} aria-hidden="true" />
              <span className={`flex h-5 w-5 shrink-0 items-center justify-center rounded-full ${presentation.iconClassName}`}>
                <AlertIcon size={13} strokeWidth={2.35} aria-hidden="true" />
              </span>
              <span className="min-w-0 flex-1">{alert.message}</span>
              {semantics.canCopy && (
                <ErrorCopyAction
                  copyText={alert.message}
                  label={tone === "error" ? "エラーをコピー" : "警告をコピー"}
                />
              )}
              <button
                type="button"
                onClick={onDismiss}
                className="inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-md text-zinc-500 transition-colors hover:bg-white/[0.06] hover:text-zinc-200"
                aria-label="アラートを閉じる"
                title="閉じる"
              >
                <X size={14} aria-hidden="true" />
              </button>
            </motion.div>
          )}
        </AnimatePresence>
      </div>
    </LayerPortal>
  );
}
