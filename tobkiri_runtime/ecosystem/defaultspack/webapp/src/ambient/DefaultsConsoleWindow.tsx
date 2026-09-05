import { useEffect, useState } from "react";
import { CheckCircle2, Loader2, RefreshCw, ShieldCheck } from "lucide-react";

import { cn } from "../lib/cn";
import { ErrorNotice } from "../components/ErrorNotice";
import { ambientTriggerClient, type AmbientStatus } from "./ambientTriggerClient";
import {
  AMBIENT_CAMERA_PERMISSION,
  AMBIENT_MIC_PERMISSION,
  AMBIENT_REQUIRED_PERMISSIONS,
  ambientPermissionLabels,
  hasAllOsPermissions,
  hasAllRumiPermissions,
  osPermissionBucket,
  permissionBucketLabel,
  rumiPermissionBucket,
  type AmbientPermissionBucket,
} from "./ambientUiState";

export function DefaultsConsoleWindow() {
  const [status, setStatus] = useState<AmbientStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = async () => {
    setLoading(true);
    setError(null);
    try {
      setStatus(await ambientTriggerClient.status());
    } catch (loadError) {
      setError(loadError instanceof Error ? loadError.message : "詳細ログを読み込めませんでした。");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    document.title = "詳細ログ";
    void refresh();
    const timer = window.setInterval(() => void refresh(), 3500);
    return () => window.clearInterval(timer);
  }, []);

  const rumiReady = hasAllRumiPermissions(status);
  const osReady = hasAllOsPermissions(status);
  const auditTail = status?.audit_tail ?? [];

  return (
    <main className="flex h-screen flex-col overflow-hidden bg-zinc-950 text-zinc-100">
      <header className="flex h-12 shrink-0 items-center justify-between gap-3 border-b border-zinc-800 px-3">
        <div className="min-w-0">
          <h1 className="truncate text-sm font-semibold">詳細ログ</h1>
          <p className="truncate text-[11px] text-zinc-500">合図待ち・指録音の状態とイベント</p>
        </div>
        <button
          type="button"
          onClick={() => void refresh()}
          disabled={loading}
          className="inline-flex h-8 w-8 items-center justify-center rounded-md border border-zinc-800 text-zinc-400 hover:border-zinc-700 hover:text-zinc-100 disabled:opacity-50"
          aria-label="再読み込み"
          title="再読み込み"
        >
          {loading ? <Loader2 size={14} className="animate-spin" /> : <RefreshCw size={14} />}
        </button>
      </header>

      {error && (
        <ErrorNotice
          className="rounded-none border-x-0 border-t-0 px-3 py-2 text-xs"
          copyLabel="Defaults コンソールエラーをコピー"
          message={error}
        />
      )}

      <div className="min-h-0 flex-1 overflow-auto px-3 py-3">
        <section className="grid gap-2 sm:grid-cols-3">
          <StatusTile label="合図待ち" ready={Boolean(status?.ambient_monitor.enabled)} readyText="使用中" idleText="停止中" />
          <StatusTile label="Tobkiri許可" ready={rumiReady} readyText="OK" idleText="確認が必要" />
          <StatusTile label="Mac許可" ready={osReady} readyText="OK" idleText="確認が必要" />
        </section>

        <section className="mt-3 rounded-lg border border-zinc-800 bg-zinc-900/35 p-3">
          <div className="flex items-center gap-2 text-xs font-semibold text-zinc-200">
            <ShieldCheck size={14} className="text-emerald-200" />
            保存しないもの
          </div>
          <p className="mt-1 text-[11px] leading-5 text-zinc-400">
            音声本文、カメラ映像、transcript全文、raw hand landmarks は保存しません。ここに出るのはイベント時刻と結果だけです。
          </p>
        </section>

        <section className="mt-3 grid gap-3 lg:grid-cols-[260px_1fr]">
          <div className="rounded-lg border border-zinc-800 bg-zinc-900/35 p-3">
            <p className="text-[11px] font-semibold text-zinc-500">許可の問題</p>
            <div className="mt-2 space-y-1.5">
              {AMBIENT_REQUIRED_PERMISSIONS
                .map((permissionId) => ({
                  permissionId,
                  label: ambientPermissionLabels[permissionId] ?? permissionId,
                  bucket: rumiPermissionBucket(status, permissionId),
                  lane: "Rumi",
                }))
                .filter((item) => item.bucket !== "granted")
                .map((item) => (
                  <PermissionProblem key={`rumi-${item.permissionId}`} {...item} />
                ))}
              {[AMBIENT_MIC_PERMISSION, AMBIENT_CAMERA_PERMISSION]
                .map((permissionId) => ({
                  permissionId,
                  label: permissionId === AMBIENT_MIC_PERMISSION ? "マイク" : "カメラ",
                  bucket: osPermissionBucket(status, permissionId),
                  lane: "Mac",
                }))
                .filter((item) => item.bucket !== "granted")
                .map((item) => (
                  <PermissionProblem key={`os-${item.permissionId}`} {...item} />
                ))}
              {rumiReady && osReady && (
                <div className="flex items-center gap-2 text-xs text-emerald-100">
                  <CheckCircle2 size={14} />
                  問題はありません
                </div>
              )}
            </div>
          </div>

          <div className="rounded-lg border border-zinc-800 bg-black/25 p-3">
            <div className="flex items-center justify-between gap-2">
              <p className="text-[11px] font-semibold text-zinc-500">イベント</p>
              <span className="text-[10px] text-zinc-600">{auditTail.length}件</span>
            </div>
            <div className="mt-2 max-h-[310px] overflow-auto font-mono text-[11px] leading-5">
              {auditTail.length === 0 && (
                <p className="font-sans text-xs text-zinc-500">まだイベントはありません。</p>
              )}
              {auditTail.map((item, index) => (
                <div key={eventKey(item, index)} className="border-b border-zinc-900 py-1.5 last:border-0">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="text-zinc-500">{eventTime(item)}</span>
                    <span className={cn("rounded px-1.5 py-0.5", eventTone(item))}>{eventStatus(item)}</span>
                    <span className="text-zinc-200">{eventTitle(item)}</span>
                  </div>
                  <p className="mt-0.5 truncate text-zinc-600">{eventDetail(item)}</p>
                </div>
              ))}
            </div>
          </div>
        </section>
      </div>
    </main>
  );
}

function StatusTile({ label, ready, readyText, idleText }: { label: string; ready: boolean; readyText: string; idleText: string }) {
  return (
    <div className={cn(
      "rounded-lg border px-3 py-2",
      ready ? "border-emerald-400/25 bg-emerald-400/10" : "border-zinc-800 bg-zinc-900/35",
    )}>
      <p className="text-[11px] text-zinc-500">{label}</p>
      <p className={cn("mt-1 text-sm font-semibold", ready ? "text-emerald-100" : "text-zinc-200")}>{ready ? readyText : idleText}</p>
    </div>
  );
}

function PermissionProblem({ label, bucket, lane }: { label: string; bucket: AmbientPermissionBucket; lane: string }) {
  return (
    <div className="flex items-center justify-between gap-2 rounded-md border border-amber-400/20 bg-amber-400/10 px-2 py-1 text-xs">
      <span className="min-w-0 truncate text-amber-50">{lane}: {label}</span>
      <span className="shrink-0 text-[11px] text-amber-200">{permissionBucketLabel(bucket)}</span>
    </div>
  );
}

function eventKey(item: Record<string, unknown>, index: number): string {
  return `${index}-${String(item.event_id || item.id || item.created_at || item.timestamp || "event")}`;
}

function eventTime(item: Record<string, unknown>): string {
  const raw = String(item.created_at || item.timestamp || item.time || "");
  if (!raw) return "--:--:--";
  const date = new Date(raw);
  if (Number.isNaN(date.getTime())) return raw.slice(0, 19);
  return new Intl.DateTimeFormat("ja-JP", { hour: "2-digit", minute: "2-digit", second: "2-digit" }).format(date);
}

function eventStatus(item: Record<string, unknown>): string {
  return String(item.status || item.reason || "event");
}

function eventTitle(item: Record<string, unknown>): string {
  return String(item.event || item.trigger || item.source || item.action_id || "ambient");
}

function eventDetail(item: Record<string, unknown>): string {
  const parts = [
    item.action_id ? `action=${String(item.action_id)}` : "",
    item.source ? `source=${String(item.source)}` : "",
    item.reason ? `reason=${String(item.reason)}` : "",
  ].filter(Boolean);
  return parts.join(" / ") || "trigger event only";
}

function eventTone(item: Record<string, unknown>): string {
  const status = eventStatus(item).toLowerCase();
  if (status.includes("failed") || status.includes("denied") || status.includes("error")) return "bg-red-500/15 text-red-100";
  if (status.includes("completed") || status.includes("dispatched") || status.includes("granted")) return "bg-emerald-500/15 text-emerald-100";
  if (status.includes("sending") || status.includes("processing")) return "bg-sky-500/15 text-sky-100";
  return "bg-zinc-800 text-zinc-300";
}
