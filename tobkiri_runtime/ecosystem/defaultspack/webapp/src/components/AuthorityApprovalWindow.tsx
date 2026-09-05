import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Check,
  ExternalLink,
  Loader2,
  RefreshCw,
  ShieldAlert,
  ShieldCheck,
  ShieldX,
  X,
} from "lucide-react";

import { ambientTriggerClient, type AmbientStatus } from "../ambient/ambientTriggerClient";
import {
  AMBIENT_AUTHORITY_REQUEST_ID,
  AMBIENT_CAMERA_PERMISSION,
  AMBIENT_MIC_PERMISSION,
  AMBIENT_OS_PERMISSIONS,
  AMBIENT_REQUIRED_PERMISSIONS,
  grantedPermissionCount,
  hasAllRumiPermissions,
} from "../ambient/ambientUiState";
import {
  interactiveApprovalResources,
  type InteractiveApprovalRequest,
} from "../features/chat/resources/authorityApprovalResources";
import { broadcastAuthorityApprovalSettlement } from "../lib/authorityApprovalEvents";
import { closeCurrentWindow, getAuthorityApprovalContext, openFingerRecordingWindow } from "../lib/desktopApproval";
import { cn } from "../lib/cn";
import { ErrorNotice } from "./ErrorNotice";

type DecisionState = "idle" | "approved" | "denied";

type TauriAuthorityWindow = Window & {
  __TAURI_INTERNALS__?: unknown;
  __TAURI__?: unknown;
};

const DISPLAY_METADATA_LABELS: Record<string, string> = {
  action: "操作内容",
  app: "アプリ",
  detail: "詳細",
  risk: "リスク",
  summary: "概要",
  title: "タイトル",
};

function ApprovalError({ message, compact = false }: { message: string; compact?: boolean }) {
  return (
    <ErrorNotice
      className={cn(
        "text-red-100",
        compact ? "p-2 text-xs" : "px-3 py-2 text-sm",
      )}
      copyLabel="承認エラーをコピー"
      errorIcon="approval"
      message={message}
      messageClassName="break-words"
    />
  );
}

function requestIdFromLocation(): string {
  try {
    return new URLSearchParams(window.location.search).get("request_id")?.trim() ?? "";
  } catch {
    return "";
  }
}

function hasNativeApprovalContext(): boolean {
  if (typeof window === "undefined") return false;
  const candidate = window as TauriAuthorityWindow;
  return Boolean(candidate.__TAURI__ || candidate.__TAURI_INTERNALS__);
}

function isPending(request: InteractiveApprovalRequest | null): boolean {
  return request?.state === "pending" || request?.state === "approval_pending";
}

function statusLabel(state: string): string {
  const labels: Record<string, string> = {
    ambiguous: "状態を確認できません",
    approval_pending: "承認待ち",
    approved: "承認済み",
    cancelled: "取り消し済み",
    denied: "拒否済み",
    expired: "期限切れ",
    failed: "失敗",
    pending: "承認待ち",
    stale: "古いリクエストです",
    succeeded: "完了",
  };
  return labels[state] ?? state;
}

function formattedExpiry(value: number): string {
  const date = new Date(value * 1000);
  return Number.isNaN(date.getTime()) ? "不明" : date.toLocaleString();
}

function approvalErrorMessage(error: unknown): string {
  const message = error instanceof Error ? error.message : String(error || "");
  if (message.includes("APPROVAL_REQUEST_MISMATCH")) {
    return "承認ウィンドウとリクエストが一致しません。操作は拒否されました。";
  }
  if (message.includes("HTTP 409")) {
    return "このリクエストはすでに処理されています。状態を再読み込みしてください。";
  }
  if (message.includes("HTTP 410") || message.includes("EXPIRED")) {
    return "このリクエストは期限切れです。";
  }
  if (message.includes("AUTHORITY_UI_OPERATOR_UNAVAILABLE")) {
    return "承認は Tobkiri Launcher の専用ウィンドウから実行してください。";
  }
  return message || "承認の処理に失敗しました。";
}

async function closeApprovalWindow(): Promise<void> {
  try {
    if (await closeCurrentWindow()) return;
  } catch {
    // The browser fallback below is intentionally side-effect free.
  }
  window.close();
}

async function returnToFingerRecordingAfterApproval(): Promise<void> {
  try {
    if (await closeCurrentWindow()) return;
  } catch {
    // Fall back to the browser close path below.
  }
  window.close();
  window.setTimeout(() => {
    if (!document.hidden) window.location.replace("/finger-recording?authority_approved=1");
  }, 250);
}

function assertMatchingRequest(
  expectedRequestId: string,
  request: InteractiveApprovalRequest,
): InteractiveApprovalRequest {
  if (request.request_id !== expectedRequestId) {
    throw new Error("APPROVAL_REQUEST_MISMATCH");
  }
  return request;
}

/**
 * The only web surface for V4 interactive approval.
 *
 * The query string supplies merely a request-id hint. Every display and every
 * decision is bound to a fresh authoritative read from the narrow Host port.
 * This component cannot dispatch a deferred effect; the Host owns that step.
 */
export function AuthorityApprovalWindow() {
  const requestId = requestIdFromLocation();
  const isAmbientPackApproval = requestId === AMBIENT_AUTHORITY_REQUEST_ID;
  const [request, setRequest] = useState<InteractiveApprovalRequest | null>(null);
  const [loading, setLoading] = useState(true);
  const [action, setAction] = useState<"approve" | "deny" | null>(null);
  const [decision, setDecision] = useState<DecisionState>("idle");
  const [confirmationText, setConfirmationText] = useState("");
  const [error, setError] = useState<string | null>(null);
  const nativeApprovalAvailable = hasNativeApprovalContext();

  const readAuthoritativeRequest = useCallback(async (): Promise<InteractiveApprovalRequest> => {
    if (!requestId) throw new Error("APPROVAL_REQUEST_MISMATCH");
    const latest = await interactiveApprovalResources.get(requestId);
    return assertMatchingRequest(requestId, latest);
  }, [requestId]);

  const refresh = useCallback(async () => {
    if (!requestId) {
      setLoading(false);
      setRequest(null);
      return;
    }
    setLoading(true);
    setError(null);
    try {
      const latest = await readAuthoritativeRequest();
      setRequest(latest);
      if (latest.state === "approved") setDecision("approved");
      if (latest.state === "denied") setDecision("denied");
    } catch (refreshError) {
      setRequest(null);
      setError(approvalErrorMessage(refreshError));
    } finally {
      setLoading(false);
    }
  }, [readAuthoritativeRequest, requestId]);

  useEffect(() => {
    if (isAmbientPackApproval) return;
    document.title = "Tobkiriの許可";
    void refresh();
  }, [isAmbientPackApproval, refresh]);

  const metadataRows = useMemo(() => {
    if (!request) return [];
    return Object.entries(request.redacted_metadata)
      .filter(([key]) => key !== "confirmation_phrase")
      .filter(([, value]) => value.trim())
      .map(([key, value]) => ({
        label: DISPLAY_METADATA_LABELS[key] ?? key,
        value,
      }));
  }, [request]);

  const confirmationPhrase = request?.redacted_metadata.confirmation_phrase?.trim() ?? "";
  const confirmationUnavailable = Boolean(
    request?.typed_confirmation_required && !confirmationPhrase,
  );

  const settleAndClose = useCallback(async (expectedState: DecisionState) => {
    const latest = await readAuthoritativeRequest();
    if (latest.state !== expectedState) {
      throw new Error("APPROVAL_REQUEST_MISMATCH");
    }
    setRequest(latest);
    setDecision(expectedState);
    window.setTimeout(() => void closeApprovalWindow(), 650);
  }, [readAuthoritativeRequest]);

  const approve = async () => {
    if (!request || !isPending(request)) return;
    if (
      request.typed_confirmation_required
      && (!confirmationPhrase || confirmationText.trim() !== confirmationPhrase)
    ) {
      setError("確認文を正しく入力してください。");
      return;
    }
    setAction("approve");
    setError(null);
    try {
      const current = await readAuthoritativeRequest();
      if (!isPending(current)) throw new Error("APPROVAL_REQUEST_MISMATCH");
      const context = await getAuthorityApprovalContext(requestId, {
        decision: "approve",
        requestSnapshotDigest: current.request_snapshot_digest,
        typedConfirmationDigest: current.typed_confirmation_digest,
      });
      const result = await interactiveApprovalResources.approve(requestId, {
        confirmation_text: confirmationText.trim(),
        ui_operator: context.ui_operator,
      });
      assertMatchingRequest(requestId, result);
      await settleAndClose("approved");
    } catch (approvalError) {
      setError(approvalErrorMessage(approvalError));
    } finally {
      setAction(null);
    }
  };

  const deny = async () => {
    if (!request || !isPending(request)) return;
    setAction("deny");
    setError(null);
    try {
      const current = await readAuthoritativeRequest();
      if (!isPending(current)) throw new Error("APPROVAL_REQUEST_MISMATCH");
      const context = await getAuthorityApprovalContext(requestId, {
        decision: "deny",
        requestSnapshotDigest: current.request_snapshot_digest,
        typedConfirmationDigest: null,
      });
      const result = await interactiveApprovalResources.deny(requestId, {
        ui_operator: context.ui_operator,
      });
      assertMatchingRequest(requestId, result);
      await settleAndClose("denied");
    } catch (denialError) {
      setError(approvalErrorMessage(denialError));
    } finally {
      setAction(null);
    }
  };

  if (isAmbientPackApproval) return <AmbientPackAuthorityApprovalWindow />;

  const controlsDisabled = loading || action !== null
    || !nativeApprovalAvailable
    || !isPending(request)
    || confirmationUnavailable;
  const confirmationSatisfied = !request?.typed_confirmation_required
    || confirmationText.trim() === confirmationPhrase;
  const settled = decision !== "idle" || !isPending(request);

  return (
    <main className="min-h-screen bg-[#09090b] text-zinc-100">
      <div className="mx-auto flex min-h-screen w-full max-w-2xl flex-col px-5 py-5">
        <header className="flex items-start justify-between gap-4 border-b border-zinc-800 pb-4">
          <div>
            <div className="flex items-center gap-2 text-[11px] font-medium text-amber-200">
              <ShieldAlert size={14} />
              Tobkiriの許可
            </div>
            <h1 className="mt-2 text-xl font-semibold text-zinc-50">この操作を許可しますか？</h1>
            <p className="mt-1 text-xs leading-5 text-zinc-500">
              許可はこの一回の保留中操作にだけ使われます。
            </p>
          </div>
          <button
            type="button"
            onClick={() => void refresh()}
            disabled={loading || action !== null}
            aria-label="承認リクエストを再読み込み"
            className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg border border-zinc-800 text-zinc-400 hover:border-zinc-700 hover:text-zinc-100 disabled:opacity-50"
            title="再読み込み"
          >
            <RefreshCw size={15} />
          </button>
        </header>

        {error && <div className="mt-4"><ApprovalError message={error} /></div>}

        <section className="mt-5 grid gap-4">
          {loading ? (
            <div role="status" className="flex min-h-56 items-center justify-center gap-2 rounded-lg border border-zinc-800 bg-zinc-950">
              <Loader2 aria-hidden="true" className="animate-spin text-zinc-500" size={22} />
              <span className="text-sm text-zinc-400">承認リクエストを読み込み中…</span>
            </div>
          ) : !requestId ? (
            <ApprovalError message="request_id が見つかりません。" />
          ) : request ? (
            <>
              <div className="rounded-lg border border-zinc-800 bg-zinc-950 p-4">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="rounded border border-zinc-800 px-2 py-1 text-[11px] text-zinc-400">
                    {statusLabel(request.state)}
                  </span>
                  <span className="rounded border border-zinc-800 px-2 py-1 text-[11px] text-zinc-500">
                    有効期限: {formattedExpiry(request.expires_at)}
                  </span>
                </div>
                {metadataRows.length > 0 && (
                  <dl className="mt-4 grid gap-3 text-xs sm:grid-cols-2">
                    {metadataRows.map((row) => (
                      <div key={row.label}>
                        <dt className="text-zinc-600">{row.label}</dt>
                        <dd className="mt-1 whitespace-pre-wrap break-words text-zinc-200">{row.value}</dd>
                      </div>
                    ))}
                  </dl>
                )}
              </div>

              {isPending(request) && request.typed_confirmation_required && (
                <div className="rounded-lg border border-amber-500/30 bg-amber-500/10 p-3">
                  <p className="text-xs font-medium text-amber-100">確認文を入力してください</p>
                  <p className="mt-1 text-xs leading-5 text-amber-100/75">
                    続けるには、次の確認文をそのまま入力してください。
                  </p>
                  <code className="mt-2 block rounded-md border border-amber-400/25 bg-black/35 px-2 py-1.5 font-mono text-xs text-amber-100">
                    {confirmationPhrase || "確認情報を取得できませんでした"}
                  </code>
                  <input
                    aria-label="承認の確認文"
                    value={confirmationText}
                    onChange={(event) => setConfirmationText(event.currentTarget.value)}
                    disabled={controlsDisabled}
                    spellCheck={false}
                    autoComplete="off"
                    className="mt-2 h-9 w-full rounded-md border border-amber-400/30 bg-black/35 px-2 font-mono text-xs text-amber-50 outline-none placeholder:text-amber-100/30 focus:border-amber-200"
                    placeholder="確認文を入力"
                  />
                </div>
              )}

              {confirmationUnavailable && (
                <ApprovalError message="この承認に必要な確認情報を取得できませんでした。安全のため操作できません。" />
              )}

              {settled ? (
                <div className={cn(
                  "rounded-lg border px-3 py-3 text-sm",
                  request.state === "approved"
                    ? "border-emerald-500/30 bg-emerald-500/10 text-emerald-100"
                    : "border-zinc-700 bg-zinc-900 text-zinc-200",
                )}>
                  <div className="flex items-center gap-2 font-medium">
                    {request.state === "approved" ? <ShieldCheck size={16} /> : <ShieldX size={16} />}
                    {statusLabel(request.state)}
                  </div>
                </div>
              ) : confirmationUnavailable ? null : !nativeApprovalAvailable ? (
                <div className="rounded-lg border border-zinc-800 bg-zinc-950 px-3 py-2 text-sm text-zinc-400">
                  承認操作は Tobkiri Launcher の専用ウィンドウで実行してください。
                </div>
              ) : (
                <div className="flex items-center justify-end gap-2">
                  <button
                    type="button"
                    onClick={() => void deny()}
                    disabled={controlsDisabled}
                    className="flex h-10 min-w-28 items-center justify-center gap-2 rounded-lg border border-zinc-800 px-4 text-sm font-semibold text-zinc-300 hover:border-red-500/40 hover:bg-red-500/10 hover:text-red-100 disabled:opacity-50"
                  >
                    {action === "deny" ? <Loader2 className="animate-spin" size={15} /> : <X size={15} />}
                    拒否
                  </button>
                  <button
                    type="button"
                    onClick={() => void approve()}
                    disabled={controlsDisabled || !confirmationSatisfied}
                    className="flex h-10 min-w-32 items-center justify-center gap-2 rounded-lg bg-zinc-100 px-4 text-sm font-semibold text-zinc-950 hover:bg-white disabled:opacity-50"
                  >
                    {action === "approve" ? <Loader2 className="animate-spin" size={15} /> : <Check size={15} />}
                    承認
                  </button>
                </div>
              )}
            </>
          ) : error ? null : <ApprovalError message="承認リクエストを取得できませんでした。" />}
        </section>
      </div>
    </main>
  );
}

function AmbientPackAuthorityApprovalWindow() {
  const [status, setStatus] = useState<AmbientStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [action, setAction] = useState<"approve" | "open" | "close" | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const closingAfterApprovalRef = useRef(false);
  const settlementBroadcastedRef = useRef(false);
  const rumiReadyRef = useRef(false);
  const rumiReady = hasAllRumiPermissions(status);
  const rumiPermissionCount = grantedPermissionCount(status, AMBIENT_REQUIRED_PERMISSIONS, "rumi");
  const osPermissionCount = grantedPermissionCount(status, AMBIENT_OS_PERMISSIONS, "os");

  const reloadStatus = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setStatus(await ambientTriggerClient.status());
    } catch (statusError) {
      setError(statusError instanceof Error ? statusError.message : "状態を取得できませんでした。");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    document.title = "Tobkiriの許可";
    void reloadStatus();
  }, [reloadStatus]);

  useEffect(() => {
    rumiReadyRef.current = rumiReady;
  }, [rumiReady]);

  const broadcastAmbientApprovalCancelled = useCallback(() => {
    if (
      settlementBroadcastedRef.current
      || closingAfterApprovalRef.current
      || rumiReadyRef.current
    ) return;
    settlementBroadcastedRef.current = true;
    broadcastAuthorityApprovalSettlement({
      requestId: AMBIENT_AUTHORITY_REQUEST_ID,
      status: "denied",
    });
  }, []);

  const finishAmbientApproval = useCallback((nextStatus?: AmbientStatus | null) => {
    if (nextStatus) setStatus(nextStatus);
    if (closingAfterApprovalRef.current) return;
    closingAfterApprovalRef.current = true;
    settlementBroadcastedRef.current = true;
    setError(null);
    setMessage("使えるようになりました。");
    broadcastAuthorityApprovalSettlement({
      requestId: AMBIENT_AUTHORITY_REQUEST_ID,
      status: "approved",
    });
    window.setTimeout(() => void returnToFingerRecordingAfterApproval(), 700);
  }, []);

  useEffect(() => {
    const settleOnClose = () => broadcastAmbientApprovalCancelled();
    window.addEventListener("pagehide", settleOnClose);
    window.addEventListener("beforeunload", settleOnClose);
    return () => {
      window.removeEventListener("pagehide", settleOnClose);
      window.removeEventListener("beforeunload", settleOnClose);
    };
  }, [broadcastAmbientApprovalCancelled]);

  useEffect(() => {
    if (!loading && rumiReady) finishAmbientApproval();
  }, [finishAmbientApproval, loading, rumiReady]);

  const approve = async () => {
    setAction("approve");
    setError(null);
    setMessage(null);
    try {
      const context = await getAuthorityApprovalContext(AMBIENT_AUTHORITY_REQUEST_ID);
      let next: AmbientStatus | null = null;
      for (const permissionId of AMBIENT_REQUIRED_PERMISSIONS) {
        next = await ambientTriggerClient.grantPermission(permissionId, {
          uiOperator: context.ui_operator,
        });
      }
      finishAmbientApproval(next ?? await ambientTriggerClient.status());
    } catch (approvalError) {
      setError(approvalError instanceof Error ? approvalError.message : "Tobkiri許可を保存できませんでした。");
    } finally {
      setAction(null);
    }
  };

  const closeWindow = async () => {
    setAction("close");
    broadcastAmbientApprovalCancelled();
    await closeApprovalWindow();
  };

  const openAmbientWindow = async () => {
    setAction("open");
    setError(null);
    try {
      const opened = await openFingerRecordingWindow();
      if (!opened) setMessage("Tobkiri Launcherから開くと、指で録音は別ウィンドウで表示されます。");
    } catch {
      setMessage("Tobkiri Launcherから開くと、指で録音は別ウィンドウで表示されます。");
    } finally {
      window.setTimeout(() => setAction(null), 300);
    }
  };

  return (
    <main className="min-h-screen bg-[#09090b] text-zinc-100">
      <div className="mx-auto flex min-h-screen w-full max-w-[520px] flex-col px-3 py-3">
        <header className="flex items-start justify-between gap-2 border-b border-zinc-800 pb-3">
          <div>
            <div className="flex items-center gap-2 text-[10px] font-medium text-amber-200">
              <ShieldAlert size={14} /> Tobkiri内の許可
            </div>
            <h1 className="mt-1 text-base font-semibold text-zinc-50">Tobkiriの許可</h1>
            <p className="mt-1 text-[11px] leading-4 text-zinc-400">
              指で録音を使えるようにします。Macのマイク・カメラ許可は別に確認されます。
            </p>
          </div>
          <button
            type="button"
            onClick={() => void reloadStatus()}
            className="flex h-8 w-8 items-center justify-center rounded-md border border-zinc-800 text-zinc-400"
            title="再読み込み"
          >
            <RefreshCw size={14} />
          </button>
        </header>
        {error && <div className="mt-3"><ApprovalError compact message={error} /></div>}
        {message && <p className="mt-3 rounded border border-emerald-500/30 bg-emerald-500/10 p-2 text-xs text-emerald-100">{message}</p>}
        <section className="mt-4 flex-1 rounded-lg border border-zinc-800 bg-zinc-950 p-3">
          {loading ? (
            <div className="flex min-h-28 items-center justify-center"><Loader2 className="animate-spin text-zinc-500" size={22} /></div>
          ) : (
            <>
              <div className="flex flex-wrap gap-2 text-[11px] text-zinc-400">
                <span>Tobkiri {rumiPermissionCount}/{AMBIENT_REQUIRED_PERMISSIONS.length}</span>
                <span>OS {osPermissionCount}/{AMBIENT_OS_PERMISSIONS.length}</span>
              </div>
              <p className="mt-4 text-sm text-zinc-200">
                {rumiReady ? "承認済みです。" : "マイク入力、カメラでのジェスチャー確認、音声の送信を許可します。"}
              </p>
            </>
          )}
        </section>
        <div className="mt-3 flex justify-end gap-2">
          <button type="button" onClick={() => void closeWindow()} disabled={action !== null} className="flex h-9 min-w-24 items-center justify-center gap-2 rounded-md border border-zinc-800 px-3 text-sm text-zinc-300 disabled:opacity-50">
            {action === "close" ? <Loader2 className="animate-spin" size={15} /> : <X size={15} />}
            閉じる
          </button>
          {rumiReady ? (
            <button
              type="button"
              onClick={() => void openAmbientWindow()}
              disabled={action !== null}
              className="flex h-9 min-w-36 items-center justify-center gap-2 rounded-md bg-emerald-200 px-3 text-sm font-semibold text-zinc-950 disabled:opacity-50"
            >
              {action === "open" ? <Loader2 className="animate-spin" size={15} /> : <ExternalLink size={15} />}
              指で録音を開く
            </button>
          ) : (
            <button type="button" onClick={() => void approve()} disabled={loading || action !== null} className="flex h-9 min-w-28 items-center justify-center gap-2 rounded-md bg-sky-300 px-3 text-sm font-semibold text-zinc-950 disabled:opacity-50">
              {action === "approve" ? <Loader2 className="animate-spin" size={15} /> : <Check size={15} />}
              許可する
            </button>
          )}
        </div>
      </div>
    </main>
  );
}
