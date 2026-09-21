import { AlertCircle, CheckCircle2, CloudOff, Loader2, Wifi, WifiOff } from "lucide-react";

import { ErrorNotice } from "../../components/ErrorNotice";
import { cn } from "../../lib/cn";
import { normalizeLocale, type LocaleSetting } from "../../lib/i18n";
import type { SettingsLoadState, SettingsSaveState } from "../types";

type SettingsStatusBarProps = {
  backendState?: "online" | "degraded" | "offline";
  backendNote?: string | null;
  saveState?: SettingsSaveState;
  loadState?: SettingsLoadState;
  locale?: LocaleSetting;
  onRetryLoad?: () => void;
  onRetrySave?: () => void;
  onOpenDirtyKey?: (key: string) => void;
};

function formatSavedTime(value: number | null | undefined, locale: LocaleSetting): string {
  if (!value) return "";
  try {
    return new Intl.DateTimeFormat(normalizeLocale(locale) === "ja" ? "ja-JP" : "en-US", {
      hour: "2-digit",
      minute: "2-digit",
    }).format(value);
  } catch {
    return "";
  }
}

export function SettingsStatusBar({
  backendState = "online",
  backendNote,
  saveState = { status: "idle", dirtyKeys: [] },
  loadState = { status: "ready" },
  locale = "ja",
  onRetryLoad,
  onRetrySave,
  onOpenDirtyKey,
}: SettingsStatusBarProps) {
  const ja = normalizeLocale(locale) === "ja";
  const copy = (english: string, japanese: string) => ja ? japanese : english;
  const dirtyCount = saveState.dirtyKeys?.length ?? 0;
  const savedTime = formatSavedTime(saveState.lastSavedAt, locale);
  const connectionLabel = backendState === "online"
    ? copy("Backend connected", "Backend接続済み")
    : backendState === "degraded"
      ? copy("Connection unstable", "接続が不安定")
      : copy("Offline protection", "オフライン保護中");
  const saveLabel = saveState.status === "saving"
    ? copy("Saving…", "保存中…")
    : saveState.status === "error"
      ? copy("Save failed", "保存失敗")
      : dirtyCount > 0
        ? copy(`${dirtyCount} unsaved`, `未保存 ${dirtyCount}件`)
        : saveState.status === "saved"
          ? savedTime
            ? copy(`Saved ${savedTime}`, `${savedTime} 保存済み`)
            : copy("Saved", "保存済み")
          : copy("No pending changes", "未保存の変更なし");

  const ConnectionIcon = backendState === "online" ? Wifi : backendState === "degraded" ? WifiOff : CloudOff;
  const SaveIcon = saveState.status === "saving" ? Loader2 : saveState.status === "error" ? AlertCircle : CheckCircle2;
  const backendMessage = backendNote || (backendState === "offline"
    ? copy("Changes remain visible locally, but they are not confirmed on the backend until reconnection.", "変更は画面上に保持されますが、再接続するまでBackendへの保存は確認されません。")
    : copy("The backend is responding intermittently. Verify the save indicator before closing Settings.", "Backendの応答が不安定です。Settingsを閉じる前に保存状態を確認してください。"));
  const saveErrorMessage = saveState.message || (dirtyCount > 0
    ? copy("Some changes remain local and have not been confirmed by the backend.", "一部の変更は画面上に保持されていますが、Backendでは未確定です。")
    : copy("The last change could not be saved and was not retained as a retryable edit.", "直前の変更を保存できず、再試行可能な編集内容としては保持されていません。"));
  const loadErrorMessage = loadState.message || copy(
    "Settings could not be refreshed. Existing values remain available.",
    "設定を再取得できませんでした。既存の値はそのまま利用できます。",
  );

  return (
    <div className="space-y-2" aria-live="polite" aria-atomic="false">
      <div className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1.5 text-[11px]">
        <span
          className={cn(
            "inline-flex min-w-0 items-center gap-1.5",
            backendState === "online" ? "text-emerald-300" : backendState === "degraded" ? "text-amber-300" : "text-red-300",
          )}
          title={backendNote ?? connectionLabel}
        >
          <ConnectionIcon size={13} aria-hidden="true" />
          <span className="truncate">{connectionLabel}</span>
        </span>
        <span className="h-3 w-px bg-white/10" aria-hidden="true" />
        <span
          className={cn(
            "inline-flex min-w-0 items-center gap-1.5",
            saveState.status === "error" ? "text-red-300" : dirtyCount > 0 || saveState.status === "saving" ? "text-amber-200" : "text-zinc-400",
          )}
          title={saveState.message ?? saveLabel}
        >
          <SaveIcon size={13} className={saveState.status === "saving" ? "animate-spin motion-reduce:animate-none" : undefined} aria-hidden="true" />
          <span className="truncate">{saveLabel}</span>
        </span>
        {loadState.status === "loading" ? (
          <>
            <span className="h-3 w-px bg-white/10" aria-hidden="true" />
            <span className="inline-flex min-w-0 items-center gap-1.5 text-zinc-500">
              <Loader2 size={13} className="animate-spin motion-reduce:animate-none" aria-hidden="true" />
              <span className="truncate">{copy("Refreshing settings…", "設定を再取得中…")}</span>
            </span>
          </>
        ) : null}
      </div>

      {backendState !== "online" ? (
        <ErrorNotice
          className="rounded-none border-y-0 border-r-0 border-l-2 px-3 py-2 text-xs leading-5"
          copyLabel={copy("Copy backend status", "Backend状態をコピー")}
          message={backendMessage}
          severity={backendState === "offline" ? "error" : "warning"}
        />
      ) : null}

      {saveState.status === "error" ? (
        <ErrorNotice
          className="rounded-none border-y-0 border-r-0 border-l-2 px-3 py-2 text-xs leading-5"
          copyLabel={copy("Copy save error", "保存エラーをコピー")}
          copyText={dirtyCount > 0
            ? `${saveErrorMessage}\n\n${copy("Unconfirmed settings", "未確定の設定")}: ${(saveState.dirtyKeys ?? []).join(", ")}`
            : saveErrorMessage}
          message={saveErrorMessage}
          trailing={dirtyCount > 0 && onRetrySave ? (
            <button
              type="button"
              onClick={onRetrySave}
              className="shrink-0 rounded-md border border-red-300/30 px-2 py-1 font-medium text-red-100 hover:bg-red-300/10"
            >
              {copy("Retry save", "保存を再試行")}
            </button>
          ) : undefined}
        >
          <div className="min-w-0">
            {dirtyCount > 0 && onOpenDirtyKey ? (
              <div className="mt-2 flex flex-wrap gap-1.5" aria-label={copy("Unconfirmed settings", "未確定の設定")}>
                {(saveState.dirtyKeys ?? []).slice(0, 3).map((key) => (
                  <button
                    key={key}
                    type="button"
                    onClick={() => onOpenDirtyKey(key)}
                    className="max-w-full truncate rounded border border-red-200/20 bg-black/15 px-2 py-0.5 font-mono text-[10px] text-red-100 hover:border-red-200/40 hover:bg-red-200/[0.07]"
                    title={key}
                  >
                    {key}
                  </button>
                ))}
                {dirtyCount > 3 ? <span className="px-1 text-[10px] text-red-200/70">{copy(`+${dirtyCount - 3} more`, `ほか ${dirtyCount - 3}件`)}</span> : null}
              </div>
            ) : null}
          </div>
        </ErrorNotice>
      ) : null}

      {loadState.status === "error" ? (
        <ErrorNotice
          className="rounded-none border-y-0 border-r-0 border-l-2 px-3 py-2 text-xs leading-5"
          copyLabel={copy("Copy settings load error", "設定読み込みエラーをコピー")}
          message={loadErrorMessage}
          trailing={onRetryLoad ? (
            <button
              type="button"
              onClick={onRetryLoad}
              className="shrink-0 rounded-md border border-red-300/30 px-2 py-1 font-medium text-red-100 hover:bg-red-300/10"
            >
              {copy("Retry", "再試行")}
            </button>
          ) : undefined}
        />
      ) : null}
    </div>
  );
}
