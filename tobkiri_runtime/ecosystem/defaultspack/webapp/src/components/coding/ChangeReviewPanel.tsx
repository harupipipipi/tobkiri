import { AlertTriangle, CheckCircle2, Download, FileSearch, GitCommit, MessageSquare, RefreshCw, RotateCw, ShieldCheck, SplitSquareHorizontal } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import type { CodingDiffResponse, CodingGitStatus } from "../../lib/api";
import type { ChangeRequestRecord } from "../../lib/changeRequests";
import {
  addChangeRequestComment,
  ChangeRequestMutationConflictError,
  changeRequestMutationContext,
  changeRequestCommitEnabled,
  commitChangeRequest,
  createChangeRequest,
  exportChangeRequestPatch,
  getChangeRequest,
  getChangeRequestSeal,
  listChangeRequestChecks,
  listChangeRequests,
  refreshChangeRequest,
  runChangeRequestCheck,
  setChangeRequestViewedFile,
  submitChangeRequestDecision,
  updateChangeRequestComment,
} from "../../lib/changeRequests";
import { codingResources } from "../../features/coding/resources/codingResources";
import { ErrorNotice } from "../ErrorNotice";
import { ChangeReviewChecksTab } from "./ChangeReviewChecksTab";
import { FilesChangedPane, filesFromStatusAndDiff } from "./FilesChangedPane";

type DetailTab = "summary" | "files" | "checks" | "review" | "commit";
type ReviewFilter = "open" | "closed";

function checkLabel(review: ChangeRequestRecord): string {
  const checks = review.check_summary;
  if (!checks) return "checks pending";
  if (checks.label) return checks.label;
  if (checks.failed) return `${checks.failed} failing`;
  if (checks.pending) return `${checks.pending} pending`;
  if (checks.passed || checks.total) return `${checks.passed ?? checks.total} passing`;
  return "checks pending";
}

function compactDate(value?: string): string {
  if (!value) return "";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

function ReviewListItem({
  review,
  selected,
  onSelect,
}: {
  review: ChangeRequestRecord;
  selected: boolean;
  onSelect: (review: ChangeRequestRecord) => void;
}) {
  return (
    <button
      type="button"
      onClick={() => onSelect(review)}
      className={`w-full rounded-md border px-2 py-1.5 text-left ${
        selected ? "border-sky-500/40 bg-sky-500/10" : "border-zinc-800 bg-zinc-950/40 hover:bg-zinc-900/70"
      }`}
    >
      <div className="flex items-center justify-between gap-2">
        <span className="min-w-0 truncate font-mono text-[11px] text-zinc-200">{review.id}</span>
        <span className="flex-shrink-0 rounded border border-zinc-800 px-1.5 py-0.5 text-[10px] text-zinc-500">{review.status}</span>
      </div>
      <div className="mt-1 flex items-center justify-between gap-2 text-[10px] text-zinc-600">
        <span className="truncate">{review.title || review.summary || "Working tree review"}</span>
        <span className="flex-shrink-0">{checkLabel(review)}</span>
      </div>
    </button>
  );
}

function shortHash(value?: string): string {
  if (!value) return "";
  return value.length > 12 ? value.slice(0, 12) : value;
}

function failedChecks(review: ChangeRequestRecord | null): number {
  return review?.check_summary?.failed ?? (review?.checks ?? []).filter((check) => ["failed", "timed_out"].includes(String(check.status))).length;
}

function reviewIsStale(review: ChangeRequestRecord | null): boolean {
  if (!review) return false;
  if (review.is_stale !== undefined) return review.is_stale === true;
  const drift = review.drift;
  return Boolean(drift?.stale ?? drift?.has_drift ?? drift?.mismatched ?? drift?.changed);
}

export function ChangeReviewPanel({ workspaceId }: { workspaceId?: string | null }) {
  const [status, setStatus] = useState<CodingGitStatus | null>(null);
  const [diff, setDiff] = useState<CodingDiffResponse | null>(null);
  const [reviews, setReviews] = useState<ChangeRequestRecord[]>([]);
  const [selectedReviewId, setSelectedReviewId] = useState<string>("working-tree");
  const [filter, setFilter] = useState<ReviewFilter>("open");
  const [detailTab, setDetailTab] = useState<DetailTab>("summary");
  const [apiAvailable, setApiAvailable] = useState(true);
  const [busy, setBusy] = useState(false);
  const [actionBusy, setActionBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [conflict, setConflict] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [commentBody, setCommentBody] = useState("");
  const [suggestionPatch, setSuggestionPatch] = useState("");
  const [checkCommand, setCheckCommand] = useState("");
  const [commitMessage, setCommitMessage] = useState("Rumi Review commit");
  const selectionRequestRef = useRef(0);
  const activeActionKeysRef = useRef(new Set<string>());
  const [pendingViewed, setPendingViewed] = useState<Record<string, boolean>>({});

  const changedFiles = useMemo(() => filesFromStatusAndDiff(status, diff), [status, diff]);
  const selectedReview = reviews.find((review) => review.id === selectedReviewId) ?? null;
  const displayFiles = selectedReview?.files?.length ? selectedReview.files : changedFiles;
  const displayDiff = selectedReview?.snapshot?.diff ?? diff?.diff ?? "";
  const dirty = !status?.clean && changedFiles.length > 0;
  const untrackedCount = changedFiles.filter((file) => file.untracked).length;
  const highRiskCount = changedFiles.filter((file) => file.highRisk).length;
  const stale = reviewIsStale(selectedReview);
  const viewedPaths = useMemo(() => new Set(
    Object.values(selectedReview?.viewed_files ?? {})
      .filter((item) => item.viewed)
      .map((item) => item.path),
  ), [selectedReview?.viewed_files]);
  const allFilesViewed = selectedReview ? displayFiles.length > 0 && displayFiles.every((file) => viewedPaths.has(file.path)) : false;
  const unresolvedCount = selectedReview?.unresolved_count ?? selectedReview?.unresolved_comment_count ?? 0;
  const failingChecks = failedChecks(selectedReview);
  const sealValid = selectedReview?.commit_seal?.valid !== false && !stale;
  const commitReady = Boolean(selectedReview && selectedReview.status === "approved" && sealValid && allFilesViewed && unresolvedCount === 0 && failingChecks === 0);
  const pendingViewedCount = Object.keys(pendingViewed).length;
  const visibleReviews = reviews.filter((review) => {
    const closed = String(review.status).toLowerCase().includes("closed");
    return filter === "closed" ? closed : !closed;
  });

  const load = useCallback(async () => {
    setBusy(true);
    setError(null);
    try {
      const [nextStatus, nextDiff, nextReviews] = await Promise.all([
        codingResources.getGitStatus({ workspace_id: workspaceId }),
        codingResources.getGitDiff({ workspace_id: workspaceId }),
        listChangeRequests({ workspace_id: workspaceId }),
      ]);
      setStatus(nextStatus);
      setDiff(nextDiff);
      setReviews(nextReviews.reviews);
      setApiAvailable(nextReviews.apiAvailable);
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      if (err instanceof ChangeRequestMutationConflictError) setConflict(message);
      else setError(message);
    } finally {
      setBusy(false);
    }
  }, [workspaceId]);

  const handleSelectReview = useCallback(async (nextReview: ChangeRequestRecord) => {
    const requestId = selectionRequestRef.current + 1;
    selectionRequestRef.current = requestId;
    setSelectedReviewId(nextReview.id);
    setDetailTab("summary");
    setError(null);
    setConflict(null);
    try {
      const hydrated = await getChangeRequest(nextReview.id);
      if (requestId !== selectionRequestRef.current) return;
      if (hydrated) {
        setReviews((items) => items.map((item) => item.id === hydrated.id ? hydrated : item));
      } else {
        setApiAvailable(false);
      }
    } catch (err) {
      if (requestId !== selectionRequestRef.current) return;
      setError(err instanceof Error ? err.message : String(err));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const handleCreateReview = async () => {
    setBusy(true);
    setError(null);
    try {
      const created = await createChangeRequest({ workspace_id: workspaceId });
      if (created) {
        setReviews((items) => [created, ...items.filter((item) => item.id !== created.id)]);
        setSelectedReviewId(created.id);
      } else {
        setApiAvailable(false);
      }
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      if (err instanceof ChangeRequestMutationConflictError) setConflict(message);
      else setError(message);
    } finally {
      setBusy(false);
    }
  };

  const handleRefreshReview = async () => {
    if (!selectedReview) return;
    setBusy(true);
    setError(null);
    try {
      const refreshed = await refreshChangeRequest(selectedReview.id, {
        workspace_id: workspaceId,
        ...changeRequestMutationContext(selectedReview, "refresh"),
      });
      if (refreshed) {
        setReviews((items) => items.map((item) => item.id === refreshed.id ? refreshed : item));
      } else {
        setApiAvailable(false);
      }
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      if (err instanceof ChangeRequestMutationConflictError) setConflict(message);
      else setError(message);
    } finally {
      setBusy(false);
    }
  };

  const replaceReview = useCallback((review: ChangeRequestRecord | null) => {
    if (!review) return;
    setReviews((items) => items.map((item) => item.id === review.id ? review : item));
  }, []);

  const runReviewAction = useCallback(async (key: string, action: () => Promise<void>) => {
    if (activeActionKeysRef.current.has(key)) return;
    activeActionKeysRef.current.add(key);
    setActionBusy(key);
    setError(null);
    setConflict(null);
    setNotice(null);
    try {
      await action();
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      if (err instanceof ChangeRequestMutationConflictError) setConflict(message);
      else setError(message);
    } finally {
      activeActionKeysRef.current.delete(key);
      setActionBusy(null);
    }
  }, []);

  const handleViewedChange = useCallback((path: string, nextViewed: boolean) => {
    if (!selectedReview) return;
    const review = selectedReview;
    const actionKey = `viewed:${review.id}:${path}`;
    setPendingViewed((current) => ({ ...current, [`${review.id}:${path}`]: nextViewed }));
    void runReviewAction(actionKey, async () => {
      try {
        const updated = await setChangeRequestViewedFile(review.id, path, nextViewed, changeRequestMutationContext(review, `viewed:${path}`));
        replaceReview(updated);
      } finally {
        setPendingViewed((current) => {
          const next = { ...current };
          delete next[`${review.id}:${path}`];
          return next;
        });
      }
    });
  }, [replaceReview, runReviewAction, selectedReview]);

  const handleAddComment = (kind: "comment" | "suggestion") => {
    if (!selectedReview) return;
    const body = commentBody.trim();
    const patch = suggestionPatch.trim();
    if (!body && !patch) {
      setError("Comment text or suggested patch is required.");
      return;
    }
    void runReviewAction("comment", async () => {
      const updated = await addChangeRequestComment(selectedReview.id, {
        ...changeRequestMutationContext(selectedReview, `comment:${kind}`),
        kind,
        body,
        suggested_patch: kind === "suggestion" ? patch : undefined,
        path: displayFiles[0]?.path,
      });
      replaceReview(updated);
      setCommentBody("");
      setSuggestionPatch("");
    });
  };

  const handleResolveComment = (commentId: string, resolved: boolean) => {
    if (!selectedReview) return;
    void runReviewAction(`resolve-${commentId}`, async () => {
      const updated = await updateChangeRequestComment(selectedReview.id, commentId, {
        ...changeRequestMutationContext(selectedReview, `comment:${commentId}`),
        resolved,
      });
      replaceReview(updated);
    });
  };

  const handleDecision = (decision: "approve" | "request_changes" | "comment") => {
    if (!selectedReview) return;
    void runReviewAction(`decision-${decision}`, async () => {
      const updated = await submitChangeRequestDecision(selectedReview.id, {
        ...changeRequestMutationContext(selectedReview, `decision:${decision}`),
        decision,
        body: decision === "comment" ? commentBody.trim() : undefined,
      });
      replaceReview(updated);
      if (decision === "comment") setCommentBody("");
    });
  };

  const handleRunCheck = (command: string) => {
    if (!selectedReview || !command.trim()) return;
    void runReviewAction(`check-${command}`, async () => {
      const result = await runChangeRequestCheck(selectedReview.id, command.trim(), changeRequestMutationContext(selectedReview, `check:${command.trim()}`));
      replaceReview(result.review);
      setCheckCommand("");
      setNotice(result.check ? `Check ${result.check.status ?? "finished"}: ${result.check.command ?? command}` : "Check finished");
    });
  };

  const handleReloadChecks = () => {
    if (!selectedReview) return;
    void runReviewAction("checks", async () => {
      const result = await listChangeRequestChecks(selectedReview.id);
      replaceReview(result.review);
    });
  };

  const handleSeal = () => {
    if (!selectedReview) return;
    void runReviewAction("seal", async () => {
      const seal = await getChangeRequestSeal(selectedReview.id);
      if (!seal) return;
      replaceReview({ ...selectedReview, commit_seal: seal });
      setNotice(seal.valid ? "Review Seal matches the current working tree." : "Review Seal mismatch blocks commit.");
    });
  };

  const handleCommit = () => {
    if (!selectedReview || !changeRequestCommitEnabled || !commitReady) return;
    void runReviewAction("commit", async () => {
      const result = await commitChangeRequest(selectedReview.id, commitMessage, changeRequestMutationContext(selectedReview, "commit"));
      if (!result) return;
      replaceReview(result.review ?? null);
      if (result.approval_required) {
        setNotice(result.display_summary || "Commit requires approval before it can run.");
        return;
      }
      if (result.blocked) {
        setNotice(`Commit blocked: ${result.reason ?? "not ready"}`);
        return;
      }
      setNotice(result.committed ? "Committed sealed snapshot locally." : "Commit did not run.");
    });
  };

  const handleExportPatch = () => {
    if (!selectedReview) return;
    void runReviewAction("export", async () => {
      const exported = await exportChangeRequestPatch(selectedReview.id);
      if (!exported?.patch) {
        setNotice("No patch is available for this review.");
        return;
      }
      const blob = new Blob([exported.patch], { type: "text/x-patch;charset=utf-8" });
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = exported.filename || `${selectedReview.id}.patch`;
      link.click();
      URL.revokeObjectURL(url);
      setNotice(`Exported ${exported.patch_bytes ?? exported.patch.length} bytes.`);
    });
  };

  return (
    <section className="border-b border-zinc-800/60 p-3" aria-label="Rumi Review">
      <div className="mb-2 flex items-center justify-between gap-2">
        <div className="flex min-w-0 items-center gap-2">
          <FileSearch size={14} className="text-teal-300" />
          <h2 className="truncate text-xs font-semibold uppercase tracking-wide text-zinc-400">Rumi Review</h2>
          <span className="truncate font-mono text-[10px] text-zinc-600">{status?.branch ?? "working tree"}</span>
        </div>
        <button
          type="button"
          disabled={busy}
          onClick={() => void load()}
          className="flex h-7 w-7 items-center justify-center rounded-md text-zinc-500 hover:bg-zinc-800 hover:text-zinc-100 disabled:opacity-40"
          title="Refresh review desk"
        >
          <RefreshCw size={13} />
        </button>
      </div>

      {error && (
        <ErrorNotice
          className="mb-2 px-2 py-1 text-[11px]"
          copyLabel="レビューのエラーをコピー"
          message={error}
        />
      )}
      {conflict && (
        <ErrorNotice
          className="mb-2 p-2 text-[11px]"
          copyLabel="レビュー競合の詳細をコピー"
          message={`${conflict} Draft text is preserved.`}
          severity="warning"
        >
          <div className="mt-1.5 flex flex-wrap gap-1.5">
            <button type="button" onClick={() => { if (selectedReview) void handleSelectReview(selectedReview); }} className="h-7 rounded border border-amber-500/30 px-2 text-[11px] hover:bg-amber-500/10">Reload latest</button>
            <button type="button" onClick={() => setDetailTab("files")} className="h-7 rounded border border-amber-500/30 px-2 text-[11px] hover:bg-amber-500/10">Compare</button>
            <button type="button" onClick={() => setConflict(null)} className="h-7 rounded border border-amber-500/30 px-2 text-[11px] hover:bg-amber-500/10">Cancel</button>
          </div>
        </ErrorNotice>
      )}
      {notice && <p className="mb-2 rounded border border-teal-500/30 bg-teal-500/10 px-2 py-1 text-[11px] text-teal-100">{notice}</p>}
      {!apiAvailable && (
        <p className="mb-2 rounded border border-amber-500/30 bg-amber-500/10 px-2 py-1 text-[11px] text-amber-100">
          Change request API is not enabled yet; working tree review remains local.
        </p>
      )}

      <div className="rounded-md border border-zinc-800 bg-zinc-950/40 p-2">
        <div className="flex items-center justify-between gap-2">
          <button
            type="button"
            onClick={() => {
              setSelectedReviewId("working-tree");
              setDetailTab("summary");
            }}
            className="min-w-0 text-left"
          >
            <p className="truncate text-xs font-semibold text-zinc-200">Working Tree</p>
            <p className="mt-0.5 text-[10px] text-zinc-600">{dirty ? "dirty" : "clean"} candidate</p>
          </button>
          <button
            type="button"
            onClick={() => void handleCreateReview()}
            disabled={busy || !dirty}
            className="h-7 flex-shrink-0 rounded-md bg-zinc-100 px-2 text-[11px] font-semibold text-zinc-950 hover:bg-white disabled:cursor-not-allowed disabled:bg-zinc-800 disabled:text-zinc-600"
          >
            Create Review
          </button>
        </div>
        <div className="mt-2 grid grid-cols-4 gap-1.5">
          <div className="rounded border border-zinc-800 bg-black/20 px-2 py-1">
            <p className="text-[10px] text-zinc-600">Files</p>
            <p className="font-mono text-xs text-zinc-200">{changedFiles.length}</p>
          </div>
          <div className="rounded border border-zinc-800 bg-black/20 px-2 py-1">
            <p className="text-[10px] text-zinc-600">Untracked</p>
            <p className="font-mono text-xs text-amber-200">{untrackedCount}</p>
          </div>
          <div className="rounded border border-zinc-800 bg-black/20 px-2 py-1">
            <p className="text-[10px] text-zinc-600">Dirty</p>
            <p className="font-mono text-xs text-zinc-200">{dirty ? "yes" : "no"}</p>
          </div>
          <div className="rounded border border-zinc-800 bg-black/20 px-2 py-1">
            <p className="text-[10px] text-zinc-600">Risk</p>
            <p className="font-mono text-xs text-red-200">{highRiskCount}</p>
          </div>
        </div>
      </div>

      <div className="mt-3 grid gap-2 min-[1440px]:grid-cols-[150px_minmax(0,1fr)]">
        <div className="space-y-2">
          <div className="flex rounded-md border border-zinc-800 bg-black/20 p-0.5">
            {(["open", "closed"] as const).map((item) => (
              <button
                key={item}
                type="button"
                onClick={() => setFilter(item)}
                className={`h-6 flex-1 rounded px-2 text-[10px] capitalize ${
                  filter === item ? "bg-zinc-800 text-zinc-100" : "text-zinc-500 hover:text-zinc-300"
                }`}
              >
                {item}
              </button>
            ))}
          </div>
          <div className="space-y-1.5">
            {visibleReviews.map((review) => (
              <ReviewListItem
                key={review.id}
                review={review}
                selected={selectedReviewId === review.id}
                onSelect={(nextReview) => void handleSelectReview(nextReview)}
              />
            ))}
            {visibleReviews.length === 0 && <p className="rounded-md border border-zinc-800 bg-black/20 px-2 py-4 text-center text-[11px] text-zinc-600">No {filter} reviews</p>}
          </div>
        </div>

        <div className="min-w-0 space-y-2">
          {stale && (
            <div className="rounded-md border border-amber-500/30 bg-amber-500/10 p-2">
              <div className="flex items-center gap-2">
                <AlertTriangle size={13} className="text-amber-200" />
                <p className="text-xs font-semibold text-amber-100">Review is stale</p>
              </div>
              <p className="mt-1 text-[11px] text-amber-100/80">working tree changed after this review was created</p>
              <div className="mt-2 flex flex-wrap gap-1.5">
                <button type="button" onClick={() => setDetailTab("files")} className="h-7 rounded-md border border-amber-500/30 px-2 text-[11px] text-amber-100 hover:bg-amber-500/10">
                  View original snapshot
                </button>
                <button type="button" onClick={() => void handleRefreshReview()} disabled={busy} className="flex h-7 items-center gap-1 rounded-md border border-amber-500/30 px-2 text-[11px] text-amber-100 hover:bg-amber-500/10 disabled:opacity-50">
                  <RotateCw size={12} /> Refresh review
                </button>
                <button type="button" onClick={() => setSelectedReviewId("working-tree")} className="flex h-7 items-center gap-1 rounded-md border border-amber-500/30 px-2 text-[11px] text-amber-100 hover:bg-amber-500/10">
                  <SplitSquareHorizontal size={12} /> Compare drift
                </button>
              </div>
            </div>
          )}

          <div className="flex flex-wrap gap-1">
            {(["summary", "files", "checks", "review", ...(changeRequestCommitEnabled ? ["commit" as const] : [])] as const).map((tab) => (
              <button
                key={tab}
                type="button"
                onClick={() => setDetailTab(tab)}
                className={`h-7 rounded-md px-2 text-[11px] capitalize ${
                  detailTab === tab ? "bg-zinc-800 text-zinc-100" : "text-zinc-500 hover:bg-zinc-900 hover:text-zinc-300"
                }`}
              >
                {tab === "files" ? "Files changed" : tab}
              </button>
            ))}
          </div>

          {detailTab === "summary" && (
            <div className="rounded-md border border-zinc-800 bg-black/20 p-3">
              <div className="flex items-center gap-2">
                <CheckCircle2 size={14} className="text-teal-300" />
                <p className="text-xs font-semibold text-zinc-200">{selectedReview?.title ?? "Working tree candidate"}</p>
              </div>
              <p className="mt-2 text-[11px] leading-relaxed text-zinc-500">
                {selectedReview?.summary ?? "Local snapshot candidate for AI review. Phase 1 shows files, risk tags, and API-backed review records when available."}
              </p>
              <div className="mt-2 flex flex-wrap gap-1">
                {selectedReview?.created_at && <span className="rounded border border-zinc-800 px-1.5 py-0.5 text-[10px] text-zinc-500">created {compactDate(selectedReview.created_at)}</span>}
                {selectedReview?.decision && <span className="rounded border border-zinc-800 px-1.5 py-0.5 text-[10px] text-zinc-500">decision {selectedReview.decision}</span>}
                {selectedReview?.unresolved_count !== undefined && <span className="rounded border border-zinc-800 px-1.5 py-0.5 text-[10px] text-zinc-500">{selectedReview.unresolved_count} unresolved</span>}
                {selectedReview?.viewed_file_count !== undefined && <span className="rounded border border-zinc-800 px-1.5 py-0.5 text-[10px] text-zinc-500">{selectedReview.viewed_file_count} viewed</span>}
                {pendingViewedCount > 0 && <span className="rounded border border-sky-500/30 bg-sky-500/10 px-1.5 py-0.5 text-[10px] text-sky-200">{pendingViewedCount} view update{pendingViewedCount === 1 ? "" : "s"} pending</span>}
                <span className="rounded border border-zinc-800 px-1.5 py-0.5 text-[10px] text-zinc-500">{checkLabel(selectedReview ?? { id: "working-tree", status: "open" })}</span>
              </div>
              {selectedReview && (
                <div className={`mt-3 grid gap-2 ${changeRequestCommitEnabled ? "min-[1440px]:grid-cols-3" : "min-[1440px]:grid-cols-2"}`}>
                  <div className={`rounded border px-2 py-1.5 ${stale ? "border-amber-500/30 bg-amber-500/10" : "border-zinc-800 bg-black/20"}`}>
                    <p className="text-[10px] text-zinc-500">Seal</p>
                    <p className={`mt-0.5 text-[11px] ${stale ? "text-amber-100" : "text-zinc-200"}`}>{stale ? "stale" : "current"}</p>
                  </div>
                  <div className={`rounded border px-2 py-1.5 ${unresolvedCount ? "border-amber-500/30 bg-amber-500/10" : "border-zinc-800 bg-black/20"}`}>
                    <p className="text-[10px] text-zinc-500">Review</p>
                    <p className={`mt-0.5 text-[11px] ${unresolvedCount ? "text-amber-100" : "text-zinc-200"}`}>{unresolvedCount ? `${unresolvedCount} unresolved` : "clear"}</p>
                  </div>
                  {changeRequestCommitEnabled && (
                    <div className={`rounded border px-2 py-1.5 ${commitReady ? "border-emerald-500/30 bg-emerald-500/10" : "border-zinc-800 bg-black/20"}`}>
                      <p className="text-[10px] text-zinc-500">Commit</p>
                      <p className={`mt-0.5 text-[11px] ${commitReady ? "text-emerald-100" : "text-zinc-200"}`}>{commitReady ? "ready" : "blocked"}</p>
                    </div>
                  )}
                </div>
              )}
            </div>
          )}
          {detailTab === "files" && (
            <FilesChangedPane
              files={displayFiles}
              diff={displayDiff}
              viewed={selectedReview ? viewedPaths : undefined}
              onViewedChange={selectedReview ? handleViewedChange : undefined}
            />
          )}
          {detailTab === "checks" && (
            <ChangeReviewChecksTab
              review={selectedReview}
              actionBusy={actionBusy}
              checkCommand={checkCommand}
              onCheckCommandChange={setCheckCommand}
              onReloadChecks={handleReloadChecks}
              onRunCheck={handleRunCheck}
            />
          )}
          {detailTab === "review" && (
            <div className="space-y-2">
              <div className="rounded-md border border-zinc-800 bg-black/20 p-2">
                <div className="flex items-center gap-2">
                  <MessageSquare size={13} className="text-teal-300" />
                  <p className="text-xs font-semibold text-zinc-200">Review comments</p>
                </div>
                <textarea
                  value={commentBody}
                  onChange={(event) => setCommentBody(event.target.value)}
                  placeholder="Leave a review comment"
                  className="mt-2 min-h-20 w-full resize-y rounded-md border border-zinc-800 bg-zinc-950 px-2 py-1.5 text-[11px] text-zinc-200 outline-none placeholder:text-zinc-700 focus:border-zinc-600"
                />
                <textarea
                  value={suggestionPatch}
                  onChange={(event) => setSuggestionPatch(event.target.value)}
                  placeholder="Suggested patch"
                  className="mt-2 min-h-16 w-full resize-y rounded-md border border-zinc-800 bg-zinc-950 px-2 py-1.5 font-mono text-[10px] text-zinc-300 outline-none placeholder:text-zinc-700 focus:border-zinc-600"
                />
                <div className="mt-2 flex flex-wrap gap-1.5">
                  <button type="button" onClick={() => handleAddComment("comment")} disabled={!selectedReview || actionBusy === "comment"} className="h-7 rounded-md border border-zinc-800 px-2 text-[11px] text-zinc-300 hover:bg-zinc-900 disabled:opacity-40">Comment</button>
                  <button type="button" onClick={() => handleAddComment("suggestion")} disabled={!selectedReview || actionBusy === "comment"} className="h-7 rounded-md border border-zinc-800 px-2 text-[11px] text-zinc-300 hover:bg-zinc-900 disabled:opacity-40">Suggest</button>
                  <button type="button" onClick={() => handleDecision("comment")} disabled={!selectedReview || actionBusy?.startsWith("decision-")} className="h-7 rounded-md border border-zinc-800 px-2 text-[11px] text-zinc-300 hover:bg-zinc-900 disabled:opacity-40">Submit comment decision</button>
                  <button type="button" onClick={() => handleDecision("request_changes")} disabled={!selectedReview || actionBusy?.startsWith("decision-")} className="h-7 rounded-md border border-red-500/30 px-2 text-[11px] text-red-200 hover:bg-red-500/10 disabled:opacity-40">Request changes</button>
                  <button type="button" onClick={() => handleDecision("approve")} disabled={!selectedReview || actionBusy?.startsWith("decision-")} className="h-7 rounded-md border border-emerald-500/30 px-2 text-[11px] text-emerald-200 hover:bg-emerald-500/10 disabled:opacity-40">Approve</button>
                </div>
              </div>
              <div className="space-y-1.5">
                {(selectedReview?.comments ?? []).map((comment) => (
                  <div key={comment.id} className="rounded-md border border-zinc-800 bg-black/20 p-2">
                    <div className="flex items-start justify-between gap-2">
                      <div className="min-w-0">
                        <p className="text-[11px] font-semibold text-zinc-200">{comment.kind ?? "comment"} {comment.path ? <span className="font-mono text-zinc-500">{comment.path}</span> : null}</p>
                        <p className="mt-1 whitespace-pre-wrap text-[11px] leading-relaxed text-zinc-400">{comment.body || "No comment body"}</p>
                      </div>
                      <button type="button" onClick={() => handleResolveComment(comment.id, !comment.resolved)} disabled={actionBusy === `resolve-${comment.id}`} className={`h-7 shrink-0 rounded-md border px-2 text-[11px] ${comment.resolved ? "border-zinc-800 text-zinc-500" : "border-emerald-500/30 text-emerald-200 hover:bg-emerald-500/10"} disabled:opacity-40`}>
                        {comment.resolved ? "Resolved" : "Resolve"}
                      </button>
                    </div>
                    {comment.suggested_patch && <pre className="mt-2 max-h-32 overflow-auto whitespace-pre-wrap rounded bg-zinc-950 p-2 font-mono text-[10px] text-zinc-500">{comment.suggested_patch}</pre>}
                  </div>
                ))}
                {selectedReview && (selectedReview.comments ?? []).length === 0 && <p className="rounded-md border border-zinc-800 bg-black/20 px-2 py-4 text-center text-[11px] text-zinc-600">No review comments</p>}
                {!selectedReview && <p className="rounded-md border border-zinc-800 bg-black/20 px-2 py-4 text-center text-[11px] text-zinc-600">Create or select a review to comment</p>}
              </div>
            </div>
          )}
          {changeRequestCommitEnabled && detailTab === "commit" && (
            <div className="space-y-2">
              <div className="rounded-md border border-zinc-800 bg-black/20 p-3">
                <div className="flex items-center gap-2">
                  <ShieldCheck size={14} className={sealValid ? "text-emerald-300" : "text-amber-300"} />
                  <p className="text-xs font-semibold text-zinc-200">Review Seal</p>
                </div>
                <div className="mt-2 grid gap-2 text-[11px] min-[1440px]:grid-cols-2">
                  <div className="rounded border border-zinc-800 bg-zinc-950 px-2 py-1">
                    <p className="text-zinc-600">snapshot</p>
                    <p className="font-mono text-zinc-300">{shortHash(selectedReview?.snapshot_working_tree_hash || selectedReview?.snapshot?.signature || selectedReview?.commit_seal?.snapshot_working_tree_hash) || "none"}</p>
                  </div>
                  <div className="rounded border border-zinc-800 bg-zinc-950 px-2 py-1">
                    <p className="text-zinc-600">current</p>
                    <p className="font-mono text-zinc-300">{shortHash(selectedReview?.current_working_tree_hash || selectedReview?.commit_seal?.current_working_tree_hash) || "not checked"}</p>
                  </div>
                </div>
                {(selectedReview?.commit_seal?.mismatch_paths ?? []).length > 0 && (
                  <div className="mt-2 rounded border border-amber-500/30 bg-amber-500/10 p-2 text-[11px] text-amber-100">
                    {(selectedReview?.commit_seal?.mismatch_paths ?? []).join(", ")}
                  </div>
                )}
                <div className="mt-2 flex flex-wrap gap-1.5">
                  <button type="button" onClick={handleSeal} disabled={!selectedReview || actionBusy === "seal"} className="flex h-7 items-center gap-1 rounded-md border border-zinc-800 px-2 text-[11px] text-zinc-300 hover:bg-zinc-900 disabled:opacity-40">
                    <RotateCw size={12} /> Recalculate
                  </button>
                  <button type="button" onClick={handleExportPatch} disabled={!selectedReview || !["approved", "committed"].includes(String(selectedReview.status)) || actionBusy === "export"} className="flex h-7 items-center gap-1 rounded-md border border-zinc-800 px-2 text-[11px] text-zinc-300 hover:bg-zinc-900 disabled:opacity-40">
                    <Download size={12} /> Export patch
                  </button>
                </div>
              </div>
              <div className="rounded-md border border-zinc-800 bg-black/20 p-3">
                <div className="flex items-center gap-2">
                  <GitCommit size={14} className={commitReady ? "text-emerald-300" : "text-zinc-500"} />
                  <p className="text-xs font-semibold text-zinc-200">{commitReady ? "Commit ready" : "Commit blocked"}</p>
                </div>
                <div className="mt-2 grid gap-1.5 text-[11px] text-zinc-500">
                  <p>{selectedReview ? `status ${selectedReview.status}` : "select a review"}</p>
                  <p>{allFilesViewed ? "all files viewed" : "not all files are viewed"}</p>
                  <p>{unresolvedCount === 0 ? "no unresolved comments" : `${unresolvedCount} unresolved comments`}</p>
                  <p>{failingChecks === 0 ? "checks are not failing" : `${failingChecks} failing checks`}</p>
                </div>
                <input
                  value={commitMessage}
                  onChange={(event) => setCommitMessage(event.target.value)}
                  className="mt-2 h-8 w-full rounded-md border border-zinc-800 bg-zinc-950 px-2 text-[11px] text-zinc-200 outline-none focus:border-zinc-600"
                />
                <button type="button" onClick={handleCommit} disabled={!selectedReview || !commitReady || !commitMessage.trim() || actionBusy === "commit"} className="mt-2 flex h-8 items-center gap-1 rounded-md bg-zinc-100 px-2 text-[11px] font-semibold text-zinc-950 hover:bg-white disabled:bg-zinc-800 disabled:text-zinc-600">
                  <GitCommit size={12} /> Commit sealed snapshot
                </button>
              </div>
            </div>
          )}
        </div>
      </div>
    </section>
  );
}
