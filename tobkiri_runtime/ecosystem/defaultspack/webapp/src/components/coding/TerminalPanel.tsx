import { Play, Shield, Terminal as TerminalIcon, Trash2 } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import type { CodingTerminalResponse } from "../../lib/api";
import { cn } from "../../lib/cn";
import { codingResources } from "../../features/coding/resources/codingResources";

type TerminalLog = CodingTerminalResponse & {
  id: string;
  cwd?: string | null;
  timeout?: number;
  replay_status?: "retrying" | "replayed";
};

export type ApprovedTerminalDecision = {
  request_id: string;
  approved?: boolean;
  token?: string;
  nonce: number;
};

function classificationTone(classification?: string): string {
  if (classification === "high" || classification === "blocked") return "text-red-300";
  if (classification === "medium") return "text-amber-300";
  return "text-emerald-300";
}

export const TERMINAL_HISTORY_POLICY = {
  durable: false,
  scope: "private-session",
  retention: "until-cleared-or-panel-closed",
} as const;

export function TerminalPanel({
  workspaceId,
  approvedDecision,
  onActionResult,
}: {
  workspaceId?: string | null;
  approvedDecision?: ApprovedTerminalDecision | null;
  onActionResult?: (result: unknown) => void;
}) {
  const [command, setCommand] = useState("git status");
  const [logs, setLogs] = useState<TerminalLog[]>([]);
  const [busy, setBusy] = useState(false);
  const handledApprovalKeys = useRef<Set<string>>(new Set());
  const sessionPendingApprovals = useRef<Map<string, TerminalLog>>(new Map());

  useEffect(() => {
    setLogs([]);
    sessionPendingApprovals.current.clear();
  }, [workspaceId]);

  const pushLog = (log: TerminalLog) => {
    if (log.approval_required && log.approval_request_id) {
      sessionPendingApprovals.current.set(log.approval_request_id, log);
    }
    setLogs((items) => [log, ...items].slice(0, 8));
  };

  const run = async () => {
    const nextCommand = command.trim();
    if (!nextCommand) return;
    if (!workspaceId) {
      pushLog({
        id: `${Date.now()}:workspace-required`,
        command: nextCommand,
        classification: "blocked",
        risk_reasons: ["Select a trusted coding workspace before running terminal commands."],
        approval_required: false,
        exit_code: null,
        stderr: "workspace required",
        workspace_id: null,
      });
      return;
    }
    const timeout = 30;
    setBusy(true);
    try {
      const result = await codingResources.runTerminalCommand(nextCommand, { workspace_id: workspaceId, timeout });
      pushLog({ ...result, id: `${Date.now()}:${nextCommand}`, timeout, workspace_id: workspaceId ?? null });
      onActionResult?.(result);
    } catch (err) {
      pushLog({
        id: `${Date.now()}:error`,
        command: nextCommand,
        classification: "error",
        risk_reasons: [err instanceof Error ? err.message : String(err)],
        approval_required: false,
        exit_code: null,
        stderr: err instanceof Error ? err.message : String(err),
      });
    } finally {
      setBusy(false);
    }
  };

  useEffect(() => {
    if (!approvedDecision?.approved || !approvedDecision.token) return;
    const key = `${approvedDecision.nonce}:${approvedDecision.request_id}`;
    if (handledApprovalKeys.current.has(key)) return;
    const pending = sessionPendingApprovals.current.get(approvedDecision.request_id);
    if (!pending) return;
    handledApprovalKeys.current.add(key);
    sessionPendingApprovals.current.delete(approvedDecision.request_id);
    const approvalToken = approvedDecision.token;

    const retry = async () => {
      setBusy(true);
      setLogs((items) => items.map((item) => (
        item.id === pending.id ? { ...item, replay_status: "retrying" } : item
      )));
      try {
        const result = await codingResources.runTerminalCommand(pending.command, {
          workspace_id: pending.workspace_id !== undefined ? pending.workspace_id : workspaceId,
          cwd: pending.cwd ?? undefined,
          timeout: pending.timeout ?? 30,
          approval_token: approvalToken,
        });
        setLogs((items) => [
          { ...result, id: `${Date.now()}:approved:${pending.command}`, replay_status: "replayed" as const },
          ...items.map((item) => (item.id === pending.id ? { ...item, replay_status: "replayed" as const } : item)),
        ].slice(0, 8));
        onActionResult?.(result);
      } catch (err) {
        pushLog({
          id: `${Date.now()}:approval-error`,
          command: pending.command,
          classification: "error",
          risk_reasons: [err instanceof Error ? err.message : String(err)],
          approval_required: false,
          exit_code: null,
          stderr: err instanceof Error ? err.message : String(err),
        });
      } finally {
        setBusy(false);
      }
    };

    void retry();
  }, [approvedDecision, onActionResult, workspaceId]);

  return (
    <section className="border-b border-zinc-800/60 p-3" aria-label="Terminal">
      <div className="mb-2 flex items-center gap-2">
        <TerminalIcon size={14} className="text-emerald-300" />
        <h2 className="truncate text-xs font-semibold uppercase tracking-wide text-zinc-400">Terminal</h2>
        <span className="ml-auto inline-flex items-center gap-1 rounded-full border border-emerald-400/20 bg-emerald-400/5 px-2 py-1 text-[10px] text-emerald-200" title="Commands and output remain only in this private panel session">
          <Shield size={10} /> Memory only
        </span>
        <button
          type="button"
          onClick={() => {
            sessionPendingApprovals.current.clear();
            setLogs([]);
          }}
          className="flex h-7 w-7 items-center justify-center rounded-md text-zinc-500 hover:bg-zinc-900 hover:text-zinc-200 disabled:opacity-40"
          aria-label="Clear terminal history from this private session"
          title="Clear terminal history"
        >
          <Trash2 size={13} />
        </button>
      </div>

      <p role="status" className="mb-2 text-[10px] leading-4 text-zinc-600">
        Private session · not saved to browser storage · cleared when this panel closes
      </p>

      <div className="flex items-center gap-1.5">
        <input
          aria-label="Terminal command"
          value={command}
          onChange={(event) => setCommand(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter") void run();
          }}
          className="h-8 min-w-0 flex-1 rounded-md border border-zinc-800 bg-zinc-950/40 px-2 font-mono text-[11px] text-zinc-300 outline-none focus:border-zinc-600"
        />
        <button
          type="button"
          disabled={busy}
          onClick={() => void run()}
          className="flex h-8 w-8 flex-shrink-0 items-center justify-center rounded-md bg-zinc-100 text-zinc-950 hover:bg-white disabled:opacity-40"
          title="Run command"
        >
          <Play size={13} />
        </button>
      </div>

      <div className="mt-2 space-y-2">
        {logs.map((log) => (
          <div key={log.id} className="rounded-md border border-zinc-800 bg-black/30 p-2">
            <div className="flex items-center justify-between gap-2">
              <code className="min-w-0 truncate text-[11px] text-zinc-200">{log.command}</code>
              <span className={cn("flex-shrink-0 text-[10px]", classificationTone(log.classification))}>
                {log.approval_required ? "approval" : log.classification || "low"}
              </span>
            </div>
            {log.risk_reasons?.length ? (
              <p className="mt-1 truncate text-[10px] text-zinc-600">{log.risk_reasons.join(", ")}</p>
            ) : null}
            <pre className="mt-1 max-h-24 overflow-auto whitespace-pre-wrap font-mono text-[10px] leading-relaxed text-zinc-500">
              {log.stdout || log.stderr || (log.replay_status === "retrying" ? "Retrying with approval" : log.approval_required ? "Approval required" : "")}
            </pre>
          </div>
        ))}
        {logs.length === 0 && <p className="py-3 text-center text-[11px] text-zinc-600">No terminal runs</p>}
      </div>
    </section>
  );
}
