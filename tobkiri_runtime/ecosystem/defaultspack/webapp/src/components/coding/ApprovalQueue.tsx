import { RefreshCw, ShieldAlert } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";

import type { CodingApprovalDecision, CodingApprovalRequest } from "../../lib/api";
import { cn } from "../../lib/cn";
import { codingResources } from "../../features/coding/resources/codingResources";
import { ErrorNotice } from "../ErrorNotice";
import { codingApprovalViewModel } from "../../lib/approvalPresentation";
import { ApprovalDecisionSurface } from "../ApprovalDecisionSurface";
import { mcpApprovalReviewRows } from "./mcpApproval";

function approvalTimestampMs(value?: number): number | null {
  if (!value) return null;
  const timestamp = value > 1_000_000_000_000 ? value : value * 1000;
  return Number.isFinite(timestamp) ? timestamp : null;
}

function formatApprovalTime(value?: number): string {
  const timestamp = approvalTimestampMs(value);
  if (timestamp === null) return "";
  const date = new Date(timestamp);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function isExpiredApproval(request: CodingApprovalRequest, now: number): boolean {
  const expiresAt = approvalTimestampMs(request.expires_at);
  return expiresAt !== null && expiresAt <= now;
}

function isActiveApproval(request: CodingApprovalRequest, now: number): boolean {
  return request.status === "pending" && !isExpiredApproval(request, now);
}

function approvalStatusLabel(request: CodingApprovalRequest, now: number): string {
  if (request.status === "pending" && isExpiredApproval(request, now)) {
    return "expired";
  }
  return request.status;
}

export function ApprovalQueue({
  initialApprovals,
  limit = 30,
  onApproved,
  onDenied,
  refreshSignal = 0,
}: {
  initialApprovals?: CodingApprovalRequest[];
  limit?: number;
  onApproved?: (decision: CodingApprovalDecision, request: CodingApprovalRequest) => void | Promise<void>;
  onDenied?: (request: CodingApprovalRequest) => void;
  refreshSignal?: number;
}) {
  const [requests, setRequests] = useState<CodingApprovalRequest[]>(initialApprovals ?? []);
  const [busy, setBusy] = useState<{ id: string; decision: "approve" | "deny" } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const activeDecisionsRef = useRef(new Set<string>());

  const load = useCallback(async () => {
    if (initialApprovals) return;
    setError(null);
    try {
      const result = await codingResources.listCodingApprovals({ limit, include_expired: true });
      setRequests(result.requests);
    } catch (err) {
      console.error(err);
      setError("承認リクエストを読み込めませんでした。接続を確認して再試行してください。");
    }
  }, [initialApprovals, limit]);

  useEffect(() => {
    void load();
  }, [load, refreshSignal]);

  const decide = async (requestId: string, decision: "approve" | "deny") => {
    if (activeDecisionsRef.current.has(requestId)) return;
    activeDecisionsRef.current.add(requestId);
    const request = requests.find((item) => item.request_id === requestId);
    setBusy({ id: requestId, decision });
    setError(null);
    try {
      if (decision === "approve") {
        const approved = await codingResources.approveCodingApproval(requestId);
        if (!approved.approved) {
          throw new Error(approved.reason || `Approval is ${approved.status || "already settled"}. Refresh and try again.`);
        }
        if (request) await onApproved?.(approved, request);
      } else {
        await codingResources.denyCodingApproval(requestId, "User denied the request from the shared approval surface");
        if (request) onDenied?.(request);
      }
      await load();
      if (initialApprovals) {
        setRequests((items) => items.map((item) => (
          item.request_id === requestId ? { ...item, status: decision === "approve" ? "approved" : "denied" } : item
        )));
      }
    } catch (err) {
      console.error(err);
      setError("判断を保存できませんでした。状態を更新してから再試行してください。");
    } finally {
      activeDecisionsRef.current.delete(requestId);
      setBusy(null);
    }
  };

  const now = Date.now();
  const visibleRequests = requests.slice(0, limit);
  const activeRequests = visibleRequests.filter((request) => isActiveApproval(request, now));
  const historyRequests = visibleRequests.filter((request) => !isActiveApproval(request, now));
  const pendingCount = activeRequests.length;

  const renderApprovalRequest = (request: CodingApprovalRequest, active: boolean) => {
    const viewModel = codingApprovalViewModel(request, now);
    if (busy?.id === request.request_id) viewModel.status = busy.decision === "approve" ? "approving" : "denying";
    return (
      <div key={request.request_id}>
        <ApprovalDecisionSurface
          approval={viewModel}
          compact={!active}
          onDeny={active ? () => void decide(request.request_id, "deny") : undefined}
          onApprove={active ? () => void decide(request.request_id, "approve") : undefined}
          className={cn(!active && "opacity-75")}
        />
        <p className="mt-1 px-1 text-[10px] text-zinc-600">{formatApprovalTime(request.created_at)} · {approvalStatusLabel(request, now)}</p>
        {mcpApprovalReviewRows(request).length > 0 && (
          <dl className="mt-2 grid gap-1 rounded border border-zinc-800/70 bg-black/20 p-2">
            {mcpApprovalReviewRows(request).map((row) => (
              <div key={row.label} className="grid grid-cols-[94px_minmax(0,1fr)] gap-2 text-[10px]">
                <dt className="text-zinc-600">{row.label}</dt>
                <dd className="break-words font-mono text-zinc-400">{row.value}</dd>
              </div>
            ))}
          </dl>
        )}
      </div>
    );
  };

  return (
    <section className="border-b border-zinc-800/60 p-3" aria-label="Approval queue">
      <div className="mb-2 flex items-center justify-between gap-2">
        <div className="flex min-w-0 items-center gap-2">
          <ShieldAlert size={14} className="text-amber-300" />
          <h2 className="truncate text-xs font-semibold uppercase tracking-wide text-zinc-400">Approvals</h2>
          <span
            className="rounded border border-zinc-800 px-1.5 py-0.5 text-[10px] text-zinc-500"
            title="Active pending approvals"
          >
            {pendingCount}
          </span>
        </div>
        <button
          type="button"
          onClick={() => void load()}
          className="flex h-7 w-7 items-center justify-center rounded-md text-zinc-500 hover:bg-zinc-800 hover:text-zinc-100"
          title="Refresh approvals"
        >
          <RefreshCw size={13} />
        </button>
      </div>

      {error && (
        <ErrorNotice
          className="mb-2 px-2 py-1 text-[11px]"
          copyLabel="承認キューのエラーをコピー"
          message={error}
        />
      )}

      <div className="space-y-2">
        {requests.length > visibleRequests.length && (
          <p className="rounded border border-zinc-800 bg-zinc-950/40 px-2 py-1 text-[11px] text-zinc-500">
            {visibleRequests.length}件を表示中（全{requests.length}件）。続きを表示するには承認一覧を更新してください。
          </p>
        )}
        {activeRequests.map((request) => renderApprovalRequest(request, true))}
        {activeRequests.length === 0 && historyRequests.length > 0 && (
          <p className="py-2 text-center text-[11px] text-zinc-600">No active approvals</p>
        )}
        {historyRequests.length > 0 && (
          <div className="space-y-2">
            <p className="px-1 text-[10px] uppercase tracking-wide text-zinc-600">Recent approval history</p>
            {historyRequests.map((request) => renderApprovalRequest(request, false))}
          </div>
        )}
        {visibleRequests.length === 0 && <p className="py-3 text-center text-[11px] text-zinc-600">No approvals</p>}
      </div>
    </section>
  );
}
