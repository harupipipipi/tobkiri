import { Clipboard, GitCompare, RefreshCw } from "lucide-react";
import { useCallback, useEffect, useId, useMemo, useState } from "react";

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
  const [copyNotice, setCopyNotice] = useState<string | null>(null);
  const headingId = `diff-panel-${useId().replace(/:/g, "")}`;

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
    <section className="border-b border-zinc-800/60 p-3" aria-labelledby={headingId} aria-busy={busy}>
      <div className="mb-2 flex items-center justify-between gap-2">
        <div className="flex min-w-0 items-center gap-2">
          <GitCompare size={14} className="text-sky-300" />
          <h2 id={headingId} className="truncate text-xs font-semibold uppercase tracking-wide text-zinc-400">Diff</h2>
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
      {copyNotice && <p role="status" aria-live="polite" className="mb-2 text-[10px] text-zinc-500">{copyNotice}</p>}

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

      <div className="mb-1 flex justify-end">
        <button
          type="button"
          onClick={() => {
            void navigator.clipboard.writeText(diff?.diff || status?.porcelain || "").then(
              () => setCopyNotice("Diff copied to clipboard."),
              () => setCopyNotice("Diff could not be copied."),
            );
          }}
          disabled={!diff?.diff && !status?.porcelain}
          aria-label={`Copy diff${status?.branch ? ` for ${status.branch}` : ""}`}
          className="flex items-center gap-1 rounded-md border border-zinc-800 px-2 text-[11px] text-zinc-400 hover:bg-zinc-900 disabled:opacity-40"
        >
          <Clipboard size={12} aria-hidden="true" /> Copy diff
        </button>
      </div>
      <pre role="region" aria-label={`Git diff${status?.branch ? ` for ${status.branch}` : ""}`} tabIndex={0} className="max-h-56 overflow-auto rounded-md border border-zinc-800 bg-black/30 p-2 font-mono text-[10px] leading-relaxed text-zinc-400">
        {diff?.diff || status?.porcelain || "No diff"}
      </pre>
    </section>
  );
}
