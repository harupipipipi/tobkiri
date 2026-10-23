import { Check, ChevronDown, LoaderCircle, PlugZap, RefreshCw, Trash2 } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";

import {
  settingsFeatureResources,
  type McpServerRecord,
} from "../../../features/settings/resources/settingsFeatureResources";
import type { SettingsFieldRendererProps } from "../fieldRendererRegistry";
import { SettingsFieldShell } from "./settingsFieldRendererUtils";

type McpLifecycleAction = "disconnect" | "reconnect" | "remove";

function serverId(server: McpServerRecord): string {
  return String(server.server_id || server.server_name || server.name || "").trim();
}

function serverState(server: McpServerRecord): string {
  if (server.connected) return "接続中";
  if (server.status) return String(server.status);
  return "登録済み";
}

function parseArgs(value: string): string[] {
  const trimmed = value.trim();
  if (!trimmed) return [];
  try {
    const parsed = JSON.parse(trimmed) as unknown;
    if (Array.isArray(parsed)) return parsed.map((entry) => String(entry));
  } catch {
    // A newline list is the easier, safe default for this form.
  }
  return trimmed.split(/\r?\n/).map((entry) => entry.trim()).filter(Boolean);
}

function messageFromError(error: unknown): string {
  return error instanceof Error ? error.message : "MCPサーバーの操作に失敗しました。";
}

/** Manage MCP registrations while preserving the backend approval boundary. */
export function McpServersField({ field }: SettingsFieldRendererProps) {
  const [servers, setServers] = useState<McpServerRecord[]>([]);
  const [name, setName] = useState("");
  const [command, setCommand] = useState("");
  const [args, setArgs] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [expandedServerId, setExpandedServerId] = useState<string | null>(null);
  const [removeCandidate, setRemoveCandidate] = useState<string | null>(null);

  const loadServers = useCallback(async () => {
    setError(null);
    try {
      const result = await settingsFeatureResources.listMcpServers();
      setServers(result.servers ?? []);
    } catch (loadError) {
      setError(messageFromError(loadError));
    }
  }, []);

  useEffect(() => {
    void loadServers();
  }, [loadServers]);

  const canConnect = useMemo(
    () => Boolean(name.trim() && command.trim()) && !busy,
    [busy, command, name],
  );

  const connect = async () => {
    if (!canConnect) return;
    const serverName = name.trim();
    setBusy(true);
    setMessage(null);
    setError(null);
    try {
      await settingsFeatureResources.registerMcpServer({
        server_id: serverName,
        name: serverName,
        config: {
          server_id: serverName,
          name: serverName,
          transport: "stdio",
          command: command.trim(),
          args: parseArgs(args),
        },
      });
      const result = await settingsFeatureResources.connectMcpServer({ server_id: serverName });
      if (result.approval_required) {
        const requestId = String(result.approval_request_id ?? result.approval_request?.request_id ?? "").trim();
        setMessage(requestId
          ? `接続前に承認が必要です。承認キューで ${requestId} を確認してから、もう一度接続してください。`
          : "接続前に承認が必要です。承認キューで内容を確認してから、もう一度接続してください。");
      } else {
        setMessage(`${serverName} を接続しました。`);
        setName("");
        setCommand("");
        setArgs("");
      }
      await loadServers();
    } catch (connectError) {
      setError(messageFromError(connectError));
    } finally {
      setBusy(false);
    }
  };

  const manage = async (server: McpServerRecord, action: McpLifecycleAction) => {
    const id = serverId(server);
    if (!id || busy) return;
    setBusy(true);
    setError(null);
    setMessage(null);
    try {
      const result = await settingsFeatureResources.manageMcpServer({
        action,
        server_id: id,
        ...(action === "remove" ? { confirm: true } : {}),
      });
      if (result.approval_required === true) {
        setMessage("この変更には承認が必要です。承認キューで確認してから、もう一度実行してください。");
      } else {
        setMessage(action === "remove" ? `${id} を削除しました。` : `${id} を${action === "disconnect" ? "切断" : "再接続"}しました。`);
      }
      setRemoveCandidate(null);
      await loadServers();
    } catch (manageError) {
      setError(messageFromError(manageError));
    } finally {
      setBusy(false);
    }
  };

  return (
    <SettingsFieldShell field={field}>
      <div className="space-y-3" data-settings-renderer="mcp_servers">
        <div className="rounded-xl border border-zinc-800 bg-zinc-950/45 p-3">
          <div className="flex items-center gap-2 text-xs font-medium text-zinc-200">
            <PlugZap size={14} className="text-zinc-400" aria-hidden="true" />
            MCPサーバーを追加
          </div>
          <div className="mt-3 grid gap-2 sm:grid-cols-2">
            <label className="space-y-1 text-[11px] text-zinc-500">
              <span>名前</span>
              <input
                value={name}
                onChange={(event) => setName(event.target.value)}
                placeholder="filesystem"
                autoComplete="off"
                className="min-h-10 w-full rounded-lg border border-zinc-800 bg-zinc-900 px-3 text-sm text-zinc-100 outline-none placeholder:text-zinc-600 focus:border-zinc-600"
              />
            </label>
            <label className="space-y-1 text-[11px] text-zinc-500">
              <span>コマンド</span>
              <input
                value={command}
                onChange={(event) => setCommand(event.target.value)}
                placeholder="npx"
                autoComplete="off"
                spellCheck={false}
                className="min-h-10 w-full rounded-lg border border-zinc-800 bg-zinc-900 px-3 font-mono text-sm text-zinc-100 outline-none placeholder:text-zinc-600 focus:border-zinc-600"
              />
            </label>
          </div>
          <label className="mt-2 block space-y-1 text-[11px] text-zinc-500">
            <span>引数（1行に1つ、またはJSON配列）</span>
            <textarea
              value={args}
              onChange={(event) => setArgs(event.target.value)}
              placeholder="-y\n@modelcontextprotocol/server-filesystem\n/Users/me/Documents"
              spellCheck={false}
              className="min-h-20 w-full resize-y rounded-lg border border-zinc-800 bg-zinc-900 px-3 py-2 font-mono text-[12px] leading-5 text-zinc-100 outline-none placeholder:text-zinc-600 focus:border-zinc-600"
            />
          </label>
          <div className="mt-3 flex flex-wrap items-center justify-between gap-2">
            <p className="max-w-xl text-[11px] leading-4 text-zinc-500">設定を登録してから、接続前に実行内容を確認します。認証情報や環境変数はこの画面に保存・表示しません。</p>
            <button
              type="button"
              disabled={!canConnect}
              onClick={() => void connect()}
              className="inline-flex min-h-9 items-center gap-1.5 rounded-lg border border-zinc-200 bg-zinc-100 px-3 text-xs font-medium text-zinc-950 transition-colors hover:bg-white disabled:cursor-not-allowed disabled:border-zinc-800 disabled:bg-zinc-900 disabled:text-zinc-600"
            >
              {busy ? <LoaderCircle size={13} className="animate-spin" aria-hidden="true" /> : <PlugZap size={13} aria-hidden="true" />}
              接続
            </button>
          </div>
        </div>

        <div className="rounded-xl border border-zinc-800 bg-zinc-950/30">
          <div className="flex items-center justify-between gap-3 border-b border-zinc-800 px-3 py-2.5">
            <span className="text-xs font-medium text-zinc-300">登録済みのMCP</span>
            <button
              type="button"
              onClick={() => void loadServers()}
              disabled={busy}
              className="inline-flex h-7 w-7 items-center justify-center rounded-md text-zinc-500 transition-colors hover:bg-zinc-800 hover:text-zinc-100 disabled:opacity-50"
              aria-label="MCP一覧を更新"
              title="更新"
            >
              <RefreshCw size={13} className={busy ? "animate-spin" : ""} aria-hidden="true" />
            </button>
          </div>
          <div className="divide-y divide-zinc-800/80">
            {servers.map((server) => {
              const id = serverId(server);
              const expanded = expandedServerId === id;
              const confirmingRemoval = removeCandidate === id;
              return (
                <div key={id || server.name} className="px-3 py-2.5">
                  <div className="flex min-w-0 items-center justify-between gap-3">
                    <button
                      type="button"
                      onClick={() => setExpandedServerId((current) => current === id ? null : id)}
                      className="flex min-w-0 items-center gap-2 text-left"
                      aria-expanded={expanded}
                    >
                      <ChevronDown size={14} className={`shrink-0 text-zinc-600 transition-transform ${expanded ? "rotate-180" : ""}`} aria-hidden="true" />
                      <span className="truncate font-mono text-xs text-zinc-200">{id}</span>
                    </button>
                    <span className={server.connected ? "shrink-0 text-[11px] text-emerald-300" : "shrink-0 text-[11px] text-zinc-500"}>{serverState(server)}</span>
                  </div>
                  {expanded && (
                    <div className="mt-2 space-y-2 pl-5">
                      <p className="break-all font-mono text-[11px] text-zinc-500">
                        {server.inspect?.command ?? String(server.config?.command ?? server.transport ?? "stdio")}
                      </p>
                      <div className="flex flex-wrap gap-2">
                        <button type="button" disabled={busy} onClick={() => void manage(server, server.connected ? "disconnect" : "reconnect")} className="rounded-md border border-zinc-700 px-2 py-1 text-[11px] text-zinc-300 transition-colors hover:bg-zinc-800 disabled:opacity-50">
                          {server.connected ? "切断" : "再接続"}
                        </button>
                        {confirmingRemoval ? (
                          <>
                            <button type="button" disabled={busy} onClick={() => void manage(server, "remove")} className="inline-flex items-center gap-1 rounded-md border border-red-500/40 bg-red-500/10 px-2 py-1 text-[11px] text-red-200 disabled:opacity-50"><Check size={12} aria-hidden="true" />削除を確定</button>
                            <button type="button" disabled={busy} onClick={() => setRemoveCandidate(null)} className="rounded-md border border-zinc-700 px-2 py-1 text-[11px] text-zinc-400 disabled:opacity-50">やめる</button>
                          </>
                        ) : (
                          <button type="button" disabled={busy} onClick={() => setRemoveCandidate(id)} className="inline-flex items-center gap-1 rounded-md px-2 py-1 text-[11px] text-red-300 transition-colors hover:bg-red-500/10 disabled:opacity-50"><Trash2 size={12} aria-hidden="true" />削除</button>
                        )}
                      </div>
                    </div>
                  )}
                </div>
              );
            })}
            {servers.length === 0 && <p className="px-3 py-5 text-center text-xs text-zinc-600">MCPはまだ登録されていません。</p>}
          </div>
        </div>
        {message && <p role="status" className="rounded-lg border border-emerald-500/20 bg-emerald-500/10 px-3 py-2 text-[11px] leading-5 text-emerald-100">{message}</p>}
        {error && <p role="alert" className="rounded-lg border border-red-500/20 bg-red-500/10 px-3 py-2 text-[11px] leading-5 text-red-100">{error}</p>}
      </div>
    </SettingsFieldShell>
  );
}
