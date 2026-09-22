import type { ButtonHTMLAttributes, ReactNode } from "react";
import { Check, Loader2 } from "lucide-react";

import { cn } from "../lib/cn";

export function LiquidPill({
  children,
  tone = "violet",
}: {
  children: ReactNode;
  tone?: "violet" | "cyan" | "mint";
}) {
  return (
    <span className="rumi-pill" data-tone={tone}>
      {children}
    </span>
  );
}

export function LiquidCard({
  children,
  className,
}: {
  children: ReactNode;
  className?: string;
}) {
  return <div className={cn("rumi-glass-card", className)}>{children}</div>;
}

export function LiquidButton({
  children,
  busy,
  quiet,
  danger,
  className,
  disabled,
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & {
  busy?: boolean;
  quiet?: boolean;
  danger?: boolean;
}) {
  return (
    <button
      {...props}
      disabled={disabled || busy}
      className={cn(
        quiet ? "rumi-quiet-button" : "rumi-liquid-button",
        danger && "rumi-danger-button",
        className,
      )}
    >
      {busy ? <Loader2 size={15} className="animate-spin" /> : null}
      {children}
    </button>
  );
}

export function ScopeChip({ label }: { label: string }) {
  return <span className="rumi-scope-chip">{label}</span>;
}

export function SecurityRow({ children }: { children: ReactNode }) {
  return <div className="rumi-security-row">{children}</div>;
}

export function StatusDots() {
  return (
    <span className="rumi-status-dots" aria-hidden="true">
      <i />
      <i />
      <i />
    </span>
  );
}

export function SoftCheck() {
  return (
    <span className="inline-grid h-5 w-5 place-items-center rounded-full bg-emerald-300 text-[11px] text-zinc-950">
      <Check size={12} strokeWidth={3} />
    </span>
  );
}
