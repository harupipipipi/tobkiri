import type { ReactNode } from "react";
import { Check } from "lucide-react";

import { cn } from "../lib/cn";
import { permissionBucketLabel, type AmbientPermissionBucket } from "./ambientUiState";

export type PermissionSectionRow = {
  id: string;
  label: string;
  bucket: AmbientPermissionBucket;
};

export function PermissionSection({
  title,
  count,
  total,
  rows,
  actionLabel,
  actionIcon,
  busyIcon,
  busy,
  disabled,
  tone = "sky",
  onAction,
}: {
  title: string;
  count: number;
  total: number;
  rows: PermissionSectionRow[];
  actionLabel: string;
  actionIcon: ReactNode;
  busyIcon: ReactNode;
  busy: boolean;
  disabled: boolean;
  tone?: "emerald" | "sky";
  onAction: () => void;
}) {
  return (
    <div className={cn("border-l pl-2", tone === "emerald" ? "border-emerald-400/40" : "border-sky-400/40")}>
      <div className="flex items-center justify-between gap-2">
        <p className="text-[12px] font-semibold text-zinc-100">{title}</p>
        <span className={cn("text-[11px]", tone === "emerald" ? "text-emerald-200" : "text-sky-200")}>{count}/{total}</span>
      </div>
      <div className="mt-2 space-y-1.5">
        {rows.map((row) => (
          <PermissionRow key={row.id} label={row.label} bucket={row.bucket} />
        ))}
      </div>
      <button type="button" onClick={onAction} disabled={disabled} className="ambient-mini-button mt-2 w-full">
        {busy ? busyIcon : actionIcon}
        {actionLabel}
      </button>
    </div>
  );
}

export function PermissionRow({ label, bucket }: { label: string; bucket: AmbientPermissionBucket }) {
  const granted = bucket === "granted";
  return (
    <div className="flex items-center justify-between gap-2 text-[12px] leading-5">
      <span className="text-zinc-300">{label}</span>
      <span className={cn("inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[11px]", granted ? "bg-emerald-400/10 text-emerald-200" : bucket === "denied" || bucket === "blocked" ? "bg-red-500/10 text-red-100" : "bg-zinc-800 text-zinc-400")}>
        {granted ? <Check size={11} /> : null}
        {permissionBucketLabel(bucket)}
      </span>
    </div>
  );
}

export function gestureStatusLabel(status: string, monitorEnabled: boolean): string {
  if (!monitorEnabled) return "未開始";
  if (status === "tracking") return "待機中";
  if (status === "recording") return "録音中";
  if (status === "reviewing") return "送信前確認";
  if (status === "transcribing") return "文字起こし中";
  if (status === "sending") return "送信中";
  if (status === "waiting_response") return "返答待ち";
  if (status === "completed") return "回答受信";
  if (status === "approval_pending") return "承認待ち";
  if (status === "error") return "エラー";
  if (status === "loading") return "準備中";
  if (status === "unavailable") return "利用不可";
  return "確認中";
}
