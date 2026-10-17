import { RefreshCw, RotateCcw, Save, ShieldAlert } from "lucide-react";
import { useCallback, useEffect, useId, useRef, useState } from "react";

import type { CodingCheckpoint, CodingDiffResponse } from "../../lib/api";
import { codingResources } from "../../features/coding/resources/codingResources";
import { ErrorNotice } from "../ErrorNotice";
import { codingActionRequiresApproval } from "./approvalQueueSync";

function checkpointLabel(checkpoint: CodingCheckpoint): string {
  return String(checkpoint.snapshot_id || checkpoint.path || "checkpoint");
}

function checkpointIdentity(checkpoint: CodingCheckpoint): string {
  return String(checkpoint.snapshot_id || checkpoint.path || "");
}

export type ApprovedCheckpointDecision = {
  request_id: string;
  approved?: boolean;
  token?: string;
  nonce: number;
};

export function codingApprovalRequestId(result: unknown): string {
  if (!result || typeof result !== "object" || Array.isArray(result)) return "";
  const record = result as Record<string, unknown>;
  const nested = record.approval_request;
  const request = nested && typeof nested === "object" && !Array.isArray(nested)
    ? nested as Record<string, unknown>
    : null;
  return String(record.approval_request_id ?? request?.request_id ?? "").trim();
}

export function CheckpointPanel({
  workspaceId,
  initialCheckpoints,
  initialDiff,
  onActionResult,
  approvedDecision,
}: {
  workspaceId?: string | null;
  initialCheckpoints?: CodingCheckpoint[];
  initialDiff?: CodingDiffResponse;
  onActionResult?: (result: unknown) => void;
  approvedDecision?: ApprovedCheckpointDecision | null;
}) {
  const selectId = useId();
  const restoreTitleId = useId();
  const restoreDescriptionId = useId();
  const [checkpoints, setCheckpoints] = useState<CodingCheckpoint[]>(initialCheckpoints ?? []);
  const [selectedSnapshotId, setSelectedSnapshotId] = useState<string>(
    initialCheckpoints?.[0]?.snapshot_id ?? "",
  );
  const [diff, setDiff] = useState<CodingDiffResponse | null>(initialDiff ?? null);
  const [pendingRestoreId, setPendingRestoreId] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [pendingApprovedRestore, setPendingApprovedRestore] = useState<{
    requestId: string;
    snapshotId: string;
    workspaceId: string | null;
  } | null>(null);
  const handledApprovalKeys = useRef<Set<string>>(new Set());

  const applyCheckpoints = useCallback((next: CodingCheckpoint[]) => {
    setCheckpoints(next);
    setSelectedSnapshotId((current) => {
      if (current && next.some((checkpoint) => checkpoint.snapshot_id === current)) return current;
      return next[0]?.snapshot_id ?? "";
    });
  }, []);

  const refreshCheckpoints = useCallback(async () => {
    const result = await codingResources.listCodingCheckpoints({ workspace_id: workspaceId, limit: 20 });
    applyCheckpoints(result.checkpoints);
    return result.checkpoints;
  }, [applyCheckpoints, workspaceId]);

  const refreshDiff = useCallback(async () => {
    const result = await codingResources.getGitDiff({ workspace_id: workspaceId });
    setDiff(result);
    return result;
  }, [workspaceId]);

  const refreshAll = useCallback(async () => {
    setBusy(true);
    setError(null);
    setMessage(null);
    try {
      await Promise.all([refreshCheckpoints(), refreshDiff()]);
      setMessage("Checkpoint and diff state refreshed");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }, [refreshCheckpoints, refreshDiff]);

  useEffect(() => {
    const next = initialCheckpoints ?? [];
    applyCheckpoints(next);
    if (initialCheckpoints === undefined) {
      void refreshCheckpoints().catch((err) => {
        setError(err instanceof Error ? err.message : String(err));
      });
    }
  }, [applyCheckpoints, initialCheckpoints, refreshCheckpoints, workspaceId]);

  useEffect(() => {
    setDiff(initialDiff ?? null);
    if (initialDiff === undefined) {
      void refreshDiff().catch((err) => {
        setError(err instanceof Error ? err.message : String(err));
      });
    }
  }, [initialDiff, refreshDiff, workspaceId]);

  useEffect(() => {
    setPendingRestoreId(null);
    setPendingApprovedRestore(null);
    setMessage(null);
    setError(null);
  }, [workspaceId]);

  const createCheckpoint = async () => {
    setBusy(true);
    setError(null);
    setMessage(null);
    try {
      const result = await codingResources.createCodingCheckpoint({
        workspace_id: workspaceId,
        paths: ["."],
        operation: "cockpit",
      });
      const created = result.checkpoint;
      const createdIdentity = checkpointIdentity(created);
      setCheckpoints((current) => [
        created,
        ...current.filter((checkpoint) => checkpointIdentity(checkpoint) !== createdIdentity),
      ]);
      setSelectedSnapshotId(created.snapshot_id ?? "");

      const refreshed = await Promise.allSettled([refreshCheckpoints(), refreshDiff()]);
      const refreshFailed = refreshed.some((entry) => entry.status === "rejected");
      setMessage(
        refreshFailed
          ? `Created ${checkpointLabel(created)}; refresh could not be confirmed`
          : `Created ${checkpointLabel(created)} and refreshed workspace state`,
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const requestRestore = () => {
    if (!selectedSnapshotId) return;
    setError(null);
    setMessage(null);
    setPendingRestoreId(selectedSnapshotId);
  };

  const restoreCheckpoint = async () => {
    if (!pendingRestoreId) return;
    const snapshotId = pendingRestoreId;
    setBusy(true);
    setError(null);
    setMessage(null);
    try {
      const result = await codingResources.restoreCodingSnapshot(snapshotId, { workspace_id: workspaceId });
      onActionResult?.(result);
      const approvalRequired = codingActionRequiresApproval(result);
      if (approvalRequired) {
        const requestId = codingApprovalRequestId(result);
        setPendingApprovedRestore(requestId ? {
          requestId,
          snapshotId,
          workspaceId: workspaceId ?? null,
        } : null);
        setMessage(`Approval required for ${snapshotId}. Review the pending request in Approvals.`);
        setPendingRestoreId(null);
        return;
      }

      const refreshed = await Promise.allSettled([refreshCheckpoints(), refreshDiff()]);
      const refreshFailed = refreshed.some((entry) => entry.status === "rejected");
      setMessage(
        refreshFailed
          ? `Restored ${snapshotId}; workspace refresh could not be confirmed`
          : `Restored ${snapshotId} and refreshed workspace state`,
      );
      setPendingRestoreId(null);
      setPendingApprovedRestore(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  useEffect(() => {
    if (!approvedDecision?.approved || !approvedDecision.token || !pendingApprovedRestore) return;
    if (approvedDecision.request_id !== pendingApprovedRestore.requestId) return;
    const key = `${approvedDecision.nonce}:${approvedDecision.request_id}`;
    if (handledApprovalKeys.current.has(key)) return;
    handledApprovalKeys.current.add(key);

    const retryRestore = async () => {
      setBusy(true);
      setError(null);
      setMessage(`Restoring ${pendingApprovedRestore.snapshotId}`);
      try {
        const result = await codingResources.restoreCodingSnapshot(
          pendingApprovedRestore.snapshotId,
          {
            workspace_id: pendingApprovedRestore.workspaceId,
            approval_token: approvedDecision.token,
          },
        );
        onActionResult?.(result);
        if (codingActionRequiresApproval(result)) {
          const requestId = codingApprovalRequestId(result);
          setPendingApprovedRestore(requestId ? {
            requestId,
            snapshotId: pendingApprovedRestore.snapshotId,
            workspaceId: pendingApprovedRestore.workspaceId,
          } : null);
          setMessage(`Approval required for ${pendingApprovedRestore.snapshotId}. Review the pending request in Approvals.`);
          return;
        }
        const refreshed = await Promise.allSettled([refreshCheckpoints(), refreshDiff()]);
        const refreshFailed = refreshed.some((entry) => entry.status === "rejected");
        setPendingApprovedRestore(null);
        setMessage(
          refreshFailed
            ? `Restored ${pendingApprovedRestore.snapshotId}; workspace refresh could not be confirmed`
            : `Restored ${pendingApprovedRestore.snapshotId} and refreshed workspace state`,
        );
      } catch (err) {
        setMessage(null);
        setError(err instanceof Error ? err.message : String(err));
      } finally {
        setBusy(false);
      }
    };

    void retryRestore();
  }, [approvedDecision, onActionResult, pendingApprovedRestore, refreshCheckpoints, refreshDiff]);

  return (
    <section className="border-b border-zinc-800/60 p-3" aria-label="Checkpoints" aria-busy={busy}>
      <div className="mb-2 flex items-center justify-between gap-2">
        <h2 className="truncate text-xs font-semibold uppercase tracking-wide text-zinc-400">Checkpoints</h2>
        <div className="flex items-center gap-1">
          <button
            type="button"
            disabled={busy}
            onClick={() => void refreshAll()}
            className="flex h-7 w-7 items-center justify-center rounded-md text-zinc-500 hover:bg-zinc-800 hover:text-zinc-100 disabled:opacity-40"
            title="Refresh checkpoints"
            aria-label={busy ? "Refreshing checkpoints" : "Refresh checkpoints"}
          >
            <RefreshCw size={13} aria-hidden="true" />
          </button>
          <button
            type="button"
            disabled={busy}
            onClick={() => void createCheckpoint()}
            className="flex h-7 w-7 items-center justify-center rounded-md text-zinc-500 hover:bg-zinc-800 hover:text-zinc-100 disabled:opacity-40"
            title="Create checkpoint"
            aria-label={busy ? "Creating checkpoint" : "Create checkpoint"}
          >
            <Save size={13} aria-hidden="true" />
          </button>
        </div>
      </div>

      {error && (
        <ErrorNotice
          className="mb-2 px-2 py-1 text-[11px]"
          copyLabel="チェックポイントのエラーをコピー"
          message={error}
        />
      )}
      {message && <p role="status" className="mb-2 rounded border border-zinc-800 bg-zinc-950 px-2 py-1 text-[11px] text-zinc-300">{message}</p>}

      <div className="flex items-center gap-1.5">
        <label htmlFor={selectId} className="sr-only">Checkpoint snapshot</label>
        <select
          id={selectId}
          value={selectedSnapshotId}
          onChange={(event) => {
            setSelectedSnapshotId(event.target.value);
            setPendingRestoreId(null);
          }}
          className="h-8 min-w-0 flex-1 rounded-md border border-zinc-800 bg-zinc-950/40 px-2 font-mono text-[11px] text-zinc-300 outline-none"
        >
          {checkpoints.map((checkpoint) => (
            <option key={checkpointIdentity(checkpoint)} value={checkpoint.snapshot_id} className="bg-zinc-900 text-zinc-100">
              {checkpointLabel(checkpoint)}
            </option>
          ))}
          {checkpoints.length === 0 && <option value="">no checkpoints</option>}
        </select>
        <button
          type="button"
          disabled={busy || !selectedSnapshotId}
          onClick={requestRestore}
          className="flex h-8 w-8 flex-shrink-0 items-center justify-center rounded-md text-amber-300 hover:bg-amber-500/10 disabled:opacity-40"
          title="Review checkpoint restore"
          aria-label={selectedSnapshotId ? `Review restore ${selectedSnapshotId}` : "Review checkpoint restore"}
        >
          <RotateCcw size={13} aria-hidden="true" />
        </button>
      </div>

      {pendingRestoreId && (
        <div
          role="alertdialog"
          aria-labelledby={restoreTitleId}
          aria-describedby={restoreDescriptionId}
          className="mt-2 rounded-md border border-amber-500/30 bg-amber-500/10 p-2"
        >
          <p id={restoreTitleId} className="text-[11px] font-semibold text-amber-100">
            Restore {pendingRestoreId}?
          </p>
          <p id={restoreDescriptionId} className="mt-1 text-[10px] leading-relaxed text-amber-100/80">
            This can overwrite or remove current workspace changes. Review the diff below and create a safety checkpoint before continuing when needed.
          </p>
          <div className="mt-2 flex justify-end gap-1.5">
            <button
              type="button"
              disabled={busy}
              onClick={() => setPendingRestoreId(null)}
              className="rounded border border-zinc-700 px-2 py-1 text-[10px] text-zinc-300 disabled:opacity-40"
            >
              Cancel
            </button>
            <button
              type="button"
              disabled={busy}
              onClick={() => void restoreCheckpoint()}
              className="rounded border border-amber-400/40 bg-amber-500/20 px-2 py-1 text-[10px] font-semibold text-amber-100 disabled:opacity-40"
            >
              {busy ? "Restoring…" : "Confirm restore"}
            </button>
          </div>
        </div>
      )}

      <div className="mt-2 rounded-md border border-zinc-800 bg-black/30 p-2">
        <div className="mb-1 flex items-center gap-1.5 text-[10px] uppercase tracking-wide text-zinc-600">
          <ShieldAlert size={11} aria-hidden="true" />
          Restore diff
        </div>
        <pre className="max-h-28 overflow-auto whitespace-pre-wrap font-mono text-[10px] leading-relaxed text-zinc-500">
          {diff?.diff || "No diff"}
        </pre>
      </div>
    </section>
  );
}
