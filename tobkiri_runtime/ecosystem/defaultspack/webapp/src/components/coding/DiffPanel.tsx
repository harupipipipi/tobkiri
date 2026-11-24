import { GitCompare, RefreshCw } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";

import type { CodingDiffResponse, CodingGitStatus } from "../../lib/api";
import { codingResources } from "../../features/coding/resources/codingResources";
import { ErrorNotice } from "../ErrorNotice";

function collectFiles(status: CodingGitStatus | null): string[] {
  if (!status) return [];
  return [...(status.staged ?? []), ...(status.modified ?? []), ...(status.untracked ?? [])];
}

export function DiffPanel({
  workspaceId,
  initialStatus,
  initialDiff,
}: {
  workspaceId?: string | null;
  initialStatus?: CodingGitStatus;
  initialDiff?: CodingDiffResponse;
}) {
  const [status, setStatus] = useState<CodingGitStatus | null>(initialStatus ?? null);
  const [diff, setDiff] = useState<CodingDiffResponse | null>(initialDiff ?? null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [refreshedAt, setRefreshedAt] = useState<number | null>(null);

  const changedFiles = useMemo(() => collectFiles(status), [status]);

  const refresh = useCallback(async () => {
    setBusy(true);
    setError(null);
    try {
      const [nextStatus, nextDiff] = await Promise.all([
        codingResources.getGitStatus({ workspace_id: workspaceId }),
        codingResources.getGitDiff({ workspace_id: workspaceId }),
      ]);
      setStatus(nextStatus);
      setDiff(nextDiff);
      setRefreshedAt(Date.now());
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }, [workspaceId]);

  useEffect(() => {
    setStatus(initialStatus ?? null);
    setDiff(initialDiff ?? null);
    setError(null);
    setRefreshedAt(null);
    if (!initialStatus || !initialDiff) void refresh();
  }, [initialDiff, initialStatus, refresh, workspaceId]);

  return (
    <section className="border-b border-zinc-800/60 p-3" aria-label="Git diff" aria-busy={busy}>
      <div className="mb-2 flex items-center justify-between gap-2">
        <div className="flex min-w-0 items-center gap-2">
          <GitCompare size={14} className="text-sky-300" />
          <h2 className="truncate text-xs font-semibold uppercase tracking-wide text-zinc-400">Diff</h2>
          {status?.branch && <span className="truncate font-mono text-[10px] text-zinc-600">{status.branch}</span>}
        </div>
        <button
          type="button"
          disabled={busy}
          onClick={() => void refresh()}
          className="flex h-7 w-7 items-center justify-center rounded-md text-zinc-500 hover:bg-zinc-800 hover:text-zinc-100 disabled:opacity-40"
          title="Refresh diff"
          aria-label={busy ? "Refreshing diff" : "Refresh diff"}
        >
          <RefreshCw size={13} aria-hidden="true" />
        </button>
      </div>

      {error && (
        <ErrorNotice
          className="mb-2 px-2 py-1 text-[11px]"
          copyLabel="差分のエラーをコピー"
          message={error}
        />
      )}
      {refreshedAt && (
        <p role="status" className="mb-2 text-[10px] text-zinc-600">
          Refreshed {new Date(refreshedAt).toLocaleTimeString()}
        </p>
      )}

      <div className="mb-2 flex flex-wrap gap-1">
        <span className="rounded border border-zinc-800 px-1.5 py-0.5 text-[10px] text-zinc-500">
          {status?.clean ? "clean" : `${changedFiles.length || diff?.files_changed || 0} files`}
        </span>
        {(changedFiles.length ? changedFiles.slice(0, 4) : diff?.files?.slice(0, 4) ?? []).map((file) => (
          <span key={file} className="max-w-[160px] truncate rounded border border-zinc-800 px-1.5 py-0.5 font-mono text-[10px] text-zinc-500">
            {file}
          </span>
        ))}
      </div>

      <pre className="max-h-56 overflow-auto rounded-md border border-zinc-800 bg-black/30 p-2 font-mono text-[10px] leading-relaxed text-zinc-400">
        {diff?.diff || status?.porcelain || "No diff"}
      </pre>
    </section>
  );
}
