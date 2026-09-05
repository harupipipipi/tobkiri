import { ExternalLink, Loader2, ShieldAlert } from "lucide-react";
import { useEffect, useId, useRef } from "react";

import type { ApprovalStatus, ApprovalViewModel } from "../lib/approvalPresentation";
import { cn } from "../lib/cn";

const STATUS_LABELS: Record<ApprovalStatus, string> = {
  pending: "判断を待っています", approving: "許可を保存しています", denying: "拒否を保存しています",
  approved: "許可済み", denied: "拒否済み", expired: "期限切れ", stale: "処理済み", error: "再試行が必要です",
};

type Props = {
  approval: ApprovalViewModel;
  onApprove?: () => void;
  onDeny?: () => void;
  onOpenTrustedWindow?: () => void;
  compact?: boolean;
  className?: string;
  keyboardShortcuts?: { approve: string; deny: string } | null;
};

export function ApprovalDecisionSurface({ approval, onApprove, onDeny, onOpenTrustedWindow, compact = false, className, keyboardShortcuts = null }: Props) {
  const headingId = useId();
  const statusRef = useRef<HTMLParagraphElement>(null);
  const pending = approval.status === "pending";
  const busy = approval.status === "approving" || approval.status === "denying";
  const actionable = pending || busy;

  useEffect(() => {
    if (!keyboardShortcuts || !pending || approval.trustedWindowRequired) return;
    const listener = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      if (target?.closest("input, textarea, select, [contenteditable='true']")) return;
      if (event.key === keyboardShortcuts.deny) { event.preventDefault(); onDeny?.(); }
      if (event.key === keyboardShortcuts.approve) { event.preventDefault(); onApprove?.(); }
    };
    document.addEventListener("keydown", listener);
    return () => document.removeEventListener("keydown", listener);
  }, [approval.trustedWindowRequired, keyboardShortcuts, onApprove, onDeny, pending]);

  return (
    <section aria-labelledby={headingId} className={cn("rounded-xl border border-amber-500/30 bg-zinc-950 p-3 shadow-2xl", className)} data-approval-source={approval.source}>
      <div className="flex items-start gap-2.5">
        <span className="mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-lg border border-amber-500/30 bg-amber-500/10 text-amber-200"><ShieldAlert size={15} /></span>
        <div className="min-w-0 flex-1">
          <p className="text-[10px] font-semibold uppercase tracking-[0.14em] text-amber-300">Tobkiri が許可を求めています</p>
          <h2 id={headingId} className="mt-1 break-words text-sm font-semibold leading-5 text-zinc-100">{approval.title}</h2>
          <p className="mt-1 break-words text-xs leading-5 text-zinc-300">{approval.consequence}</p>
        </div>
      </div>

      {!compact && (
        <dl className="mt-3 grid gap-2 rounded-lg border border-zinc-800 bg-black/25 p-3 text-xs sm:grid-cols-2">
          <div className="sm:col-span-2"><dt className="text-zinc-500">対象</dt><dd className="mt-0.5 break-all text-zinc-200">{approval.target}</dd></div>
          <div><dt className="text-zinc-500">必要な理由</dt><dd className="mt-0.5 leading-5 text-zinc-300">{approval.reason}</dd></div>
          <div><dt className="text-zinc-500">影響とリスク</dt><dd className="mt-0.5 leading-5 text-zinc-300">{approval.riskExplanation}</dd></div>
          <div><dt className="text-zinc-500">許可範囲</dt><dd className="mt-0.5 text-zinc-300">{approval.scope}</dd></div>
          <div><dt className="text-zinc-500">有効期間</dt><dd className="mt-0.5 text-zinc-300">{approval.persistence}</dd></div>
          <div className="sm:col-span-2"><dt className="text-zinc-500">記録</dt><dd className="mt-0.5 text-zinc-300">{approval.auditText}</dd></div>
        </dl>
      )}

      <details className="mt-2 text-[11px] text-zinc-500">
        <summary className="cursor-pointer select-none rounded py-1 hover:text-zinc-300 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-zinc-400">技術的な詳細</summary>
        <pre className="mt-1 max-h-40 overflow-auto whitespace-pre-wrap break-all rounded-md border border-zinc-800 bg-black/30 p-2 font-mono">{JSON.stringify(approval.technicalDetails, null, 2)}</pre>
      </details>

      <p ref={statusRef} role="status" aria-live="polite" className="mt-2 text-[11px] text-zinc-500">{STATUS_LABELS[approval.status]}</p>

      {actionable && approval.trustedWindowRequired && onOpenTrustedWindow && (
        <div className="mt-3 rounded-lg border border-sky-500/25 bg-sky-500/5 p-2.5">
          <p className="text-xs leading-5 text-zinc-300">安全な許可範囲を選ぶため、Tobkiri Launcher の専用ウィンドウで判断します。内容と会話の位置は引き継がれます。</p>
          <button type="button" disabled={busy} onClick={onOpenTrustedWindow} className="mt-2 flex min-h-10 w-full items-center justify-center gap-2 rounded-lg bg-zinc-100 px-3 text-xs font-semibold text-zinc-950 hover:bg-white disabled:opacity-50 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-white"><ExternalLink size={14} />専用ウィンドウで確認</button>
        </div>
      )}

      {actionable && !approval.trustedWindowRequired && (onApprove || onDeny) && (
        <div className="mt-3 grid grid-cols-1 gap-2 sm:grid-cols-2">
          <button type="button" disabled={busy} onClick={onDeny} className="min-h-10 rounded-lg border border-zinc-700 px-4 text-xs font-semibold text-zinc-300 hover:border-red-500/50 hover:bg-red-500/10 hover:text-red-100 disabled:opacity-50">{approval.status === "denying" && <Loader2 className="mr-1 inline animate-spin" size={13} />}拒否{keyboardShortcuts ? `（${keyboardShortcuts.deny}）` : ""}</button>
          <button type="button" disabled={busy} onClick={onApprove} className="min-h-10 rounded-lg bg-zinc-100 px-4 text-xs font-semibold text-zinc-950 hover:bg-white disabled:opacity-50">{approval.status === "approving" && <Loader2 className="mr-1 inline animate-spin" size={13} />}許可{keyboardShortcuts ? `（${keyboardShortcuts.approve}）` : ""}</button>
          {keyboardShortcuts && <p className="text-[10px] leading-4 text-zinc-500 sm:col-span-2">入力欄にフォーカスがない場合のみ、数字キー {keyboardShortcuts.deny} / {keyboardShortcuts.approve} を使用できます。</p>}
        </div>
      )}
    </section>
  );
}
