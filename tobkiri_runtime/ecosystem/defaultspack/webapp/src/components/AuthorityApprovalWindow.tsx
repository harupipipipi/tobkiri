import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Check, ExternalLink, Loader2, RefreshCw, ShieldAlert, ShieldCheck, ShieldX, X } from "lucide-react";

import { ambientTriggerClient, type AmbientStatus } from "../ambient/ambientTriggerClient";
import {
  AMBIENT_AUTHORITY_REQUEST_ID,
  AMBIENT_CAMERA_PERMISSION,
  AMBIENT_MIC_PERMISSION,
  AMBIENT_OS_PERMISSIONS,
  AMBIENT_REQUIRED_PERMISSIONS,
  ambientPermissionLabels,
  grantedPermissionCount,
  hasAllRumiPermissions,
} from "../ambient/ambientUiState";
import { authorityApprovalResources, type AuthorityApprovalDecision, type AuthorityRequest } from "../features/chat/resources/authorityApprovalResources";
import {
  authorityApprovalConfig,
  authorityApprovalSettledLabel,
  authorityApprovalShouldRetryWithFreshContext,
  authorityApprovalRiskTone,
  authorityApprovalRuntimeContent,
  authorityApprovalTitle,
  authorityRelatedPermissions,
  resolvePendingAuthorityApproval,
  type AuthorityApproval,
  type AuthorityApprovalSettledStatus,
  type AuthorityApprovalScope,
  authorityRequestSettledStatus,
} from "../lib/authorityApproval";
import {
  safeSameOriginApprovalPath,
} from "../lib/authorityApprovalBrowserToken";
import { broadcastAuthorityApprovalSettlement } from "../lib/authorityApprovalEvents";
import { closeCurrentWindow, getAuthorityApprovalContext, openFingerRecordingWindow } from "../lib/desktopApproval";
import { cn } from "../lib/cn";
import { authorityApprovalViewModel } from "../lib/approvalPresentation";
import { ApprovalDecisionSurface } from "./ApprovalDecisionSurface";

type DecisionState =
  | { kind: "idle" }
  | { kind: "approved"; decision?: AuthorityApprovalDecision; resumed: boolean }
  | { kind: "rejected" };

type AuthoritySettlement = {
  request: AuthorityRequest;
  status: AuthorityApprovalSettledStatus;
};

type TauriAuthorityWindow = Window & {
  __TAURI_INTERNALS__?: unknown;
  __TAURI__?: unknown;
};

const APPROVAL_RETURN_TO_PATHS = ["/finger-recording", "/ambient-debug"] as const;

const SCOPE_LABELS: Record<AuthorityApprovalScope, string> = {
  once: "今回のみ",
  conversation: "会話",
  profile: "プロファイル",
  node: "ノード",
};

function requestIdFromLocation(): string {
  try {
    return new URLSearchParams(window.location.search).get("request_id")?.trim() ?? "";
  } catch {
    return "";
  }
}

function isApprovalReturnToPathAllowed(pathname: string): boolean {
  return APPROVAL_RETURN_TO_PATHS.some(
    (allowedPath) => pathname === allowedPath || pathname.startsWith(`${allowedPath}/`),
  );
}

function approvalReturnToFromLocation(): string {
  try {
    const value = new URLSearchParams(window.location.search).get("return_to")?.trim() ?? "";
    if (!value) return "";
    const url = new URL(value, window.location.origin);
    if (url.origin !== window.location.origin) return "";
    if (!isApprovalReturnToPathAllowed(url.pathname)) {
      return "";
    }
    return `${url.pathname}${url.search}${url.hash}`;
  } catch {
    return "";
  }
}

function requestToApproval(request: AuthorityRequest): AuthorityApproval {
  return {
    requestId: request.request_id,
    principalId: request.principal_id,
    permissionId: request.permission_id,
    resource: request.resource ?? {},
    riskLevel: request.risk_level || request.display_metadata?.risk_level,
    summary: request.display_metadata?.summary || request.reason,
    reason: request.reason,
  };
}

function stableJson(value: unknown): string {
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

function formattedDate(value: string | null | undefined): string {
  if (!value) return "なし";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString();
}

async function returnToFingerRecordingAfterApproval() {
  try {
    if (await closeCurrentWindow()) return;
  } catch {
    // Fall back below when the approval page is not inside Tobkiri Launcher.
  }
  window.close();
  window.setTimeout(() => {
    if (document.hidden) return;
    window.location.replace("/finger-recording");
  }, 250);
}

async function closeAuthorityApprovalWindow(fallbackReturnTo = "") {
  try {
    if (await closeCurrentWindow()) return;
  } catch {
    // Fall back below when the approval page is not inside Tobkiri Launcher.
  }
  window.close();
  if (!fallbackReturnTo) return;
  window.setTimeout(() => {
    if (document.hidden) return;
    const safeReturnTo = safeSameOriginApprovalPath(fallbackReturnTo);
    if (safeReturnTo) window.location.replace(safeReturnTo);
  }, 250);
}

function scheduleAuthorityApprovalWindowClose(fallbackReturnTo = "") {
  window.setTimeout(() => void closeAuthorityApprovalWindow(fallbackReturnTo), 650);
}

function hasNativeAuthorityApprovalContext(): boolean {
  if (typeof window === "undefined") return false;
  const maybeWindow = window as TauriAuthorityWindow;
  return Boolean(maybeWindow.__TAURI__ || maybeWindow.__TAURI_INTERNALS__);
}

function stringValue(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

function stringListValue(value: unknown): string {
  if (!Array.isArray(value)) return "";
  return value.map((item) => stringValue(item)).filter(Boolean).join("\n");
}

function authorityHostExecutionSummary(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

function authorityApprovalErrorMessage(error: unknown): string {
  const message = error instanceof Error ? error.message : String(error || "");
  if (message.includes("AUTHORITY_BROWSER_TEST_DISABLED")) {
    return "このDefaultspackではブラウザ承認が無効です。Tobkiri Launcherの専用ウィンドウから開き直してください。";
  }
  if (message.includes("EXPIRED") || message.includes("HTTP 410")) {
    return "承認セッションの有効期限が切れたか、取り消されました。再読み込みしてやり直してください。";
  }
  if (message.includes("WRONG_REQUEST") || message.includes("HTTP 403")) {
    return "承認セッションがこのリクエストまたはウィンドウと一致しません。操作は拒否されました。";
  }
  if (message.includes("HTTP 409")) {
    return "この承認セッションはすでに使用されています。再読み込みしてやり直してください。";
  }
  if (message.includes("AUTHORITY_UI_OPERATOR_UNAVAILABLE")) {
    return "承認操作に必要なTobkiri Launcherの署名secretがありません。Tobkiri Launcherから起動した承認ウィンドウで承認するか、ブラウザQA用に同じ署名secretを渡して起動し直してください。";
  }
  return message || "authority 承認に失敗しました。";
}

function windowTitle(request: AuthorityRequest | null): string {
  if (!request) return "Tobkiriの許可";
  return request.display_metadata?.title || authorityApprovalTitle(requestToApproval(request));
}

export function AuthorityApprovalWindow() {
  const [requestId, setRequestId] = useState(requestIdFromLocation());
  const isAmbientPackApproval = requestId === AMBIENT_AUTHORITY_REQUEST_ID;
  const [request, setRequest] = useState<AuthorityRequest | null>(null);
  const [pendingRequests, setPendingRequests] = useState<AuthorityRequest[]>([]);
  const [selectedScope, setSelectedScope] = useState<AuthorityApprovalScope>("once");
  const [loading, setLoading] = useState(false);
  const [refreshNonce, setRefreshNonce] = useState(0);
  const [action, setAction] = useState<"approve" | "reject" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [decisionState, setDecisionState] = useState<DecisionState>({ kind: "idle" });
  const [ambientEnabled, setAmbientEnabled] = useState(false);
  const [confirmationText, setConfirmationText] = useState("");
  const [nativeApprovalAvailable, setNativeApprovalAvailable] = useState(hasNativeAuthorityApprovalContext);
  const nativeApprovalAvailableRef = useRef(nativeApprovalAvailable);
  const requestIdRef = useRef(requestId);
  const locallySettledRequestsRef = useRef(new Map<string, AuthorityApprovalSettledStatus>());
  const settlementBroadcastedRef = useRef(new Set<string>());

  const approval = useMemo(() => request ? requestToApproval(request) : null, [request]);
  const title = request ? windowTitle(request) : "Tobkiriの許可";
  const detailRows = useMemo(() => {
    if (!request) return [];
    const resource = request.resource ?? {};
    const metadata = request.display_metadata ?? {};
    const hostExecutionSummary = authorityHostExecutionSummary(metadata.host_execution_summary);
    return [
      { label: "アプリ", value: metadata.app_display_name || metadata.pack_id || stringValue(resource.app_display_name) || stringValue(resource.pack_id) },
      { label: "提供元", value: metadata.provider_display_name || metadata.provider_id || stringValue(resource.provider_display_name) || stringValue(resource.provider_id) },
      { label: "モデル", value: metadata.model_display_name || metadata.model_id || stringValue(resource.model_display_name) || stringValue(resource.model_id) },
      { label: "API key", value: metadata.credential_label || stringValue(resource.credential_label) || "secret value is never shown" },
      { label: "接続先", value: metadata.endpoint_url || stringValue(resource.endpoint_url) || metadata.endpoint_host || stringValue(resource.domain) },
      { label: "操作内容", value: metadata.access_summary || stringValue(resource.access_summary) },
      { label: "実行ファイル", value: stringValue(hostExecutionSummary.executable) },
      {
        label: "引数",
        value: typeof hostExecutionSummary.argument_count === "number" ? `${hostExecutionSummary.argument_count}` : "",
      },
      { label: "作業フォルダ", value: stringValue(hostExecutionSummary.cwd) },
      { label: "対象path", value: stringListValue(hostExecutionSummary.target_paths) },
      { label: "対象URL", value: stringListValue(hostExecutionSummary.target_urls) },
      { label: "有効期限", value: formattedDate(request.expires_at) },
    ].filter((row) => row.value);
  }, [request]);
  const allowedScopes = useMemo<AuthorityApprovalScope[]>(() => {
    const scopes = request?.allowed_scopes?.filter((scope): scope is AuthorityApprovalScope => (
      scope === "once" || scope === "conversation" || scope === "profile" || scope === "node"
    )) ?? [];
    return scopes.length ? scopes : ["once"];
  }, [request?.allowed_scopes]);
  const decisionSettledStatus: AuthorityApprovalSettledStatus | null = decisionState.kind === "approved"
    ? "approved"
    : decisionState.kind === "rejected" ? "denied" : null;
  const displayedSettledStatus = decisionSettledStatus ?? authorityRequestSettledStatus(request?.status);
  const approvalContextAvailable = nativeApprovalAvailable;
  const showApprovalControls = Boolean(request && request.status === "pending" && approvalContextAvailable && !displayedSettledStatus && decisionState.kind === "idle");
  const showPendingRequestPicker = pendingRequests.length > 0 && (!requestId || pendingRequests.length > 1);
  const controlsDisabled = !showApprovalControls || action !== null;
  const confirmationPhrase = stringValue(request?.display_metadata?.confirmation_phrase) || stringValue(request?.resource?.confirmation_phrase);
  const typedConfirmationRequired = Boolean(
    request?.display_metadata?.typed_confirmation_required
    || request?.resource?.typed_confirmation_required
    || confirmationPhrase,
  );
  const typedConfirmationSatisfied = !typedConfirmationRequired || Boolean(confirmationPhrase && confirmationText.trim() === confirmationPhrase);

  useEffect(() => {
    if (isAmbientPackApproval) return;
    document.title = title;
  }, [isAmbientPackApproval, title]);

  useEffect(() => {
    let cancelled = false;
    const loadAmbient = async () => {
      try {
        const status = await ambientTriggerClient.status();
        if (!cancelled) setAmbientEnabled(Boolean(status.ambient_monitor.enabled));
      } catch {
        if (!cancelled) setAmbientEnabled(false);
      }
    };
    void loadAmbient();
    const timer = window.setInterval(loadAmbient, 2500);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, []);

  useEffect(() => {
    if (!allowedScopes.includes(selectedScope)) {
      setSelectedScope(allowedScopes[0] ?? "once");
    }
  }, [allowedScopes, selectedScope]);

  useEffect(() => {
    setConfirmationText("");
  }, [request?.request_id]);

  useEffect(() => {
    const nextNativeApprovalAvailable = hasNativeAuthorityApprovalContext();
    nativeApprovalAvailableRef.current = nextNativeApprovalAvailable;
    setNativeApprovalAvailable(nextNativeApprovalAvailable);
  }, []);

  useEffect(() => {
    requestIdRef.current = requestId;
  }, [requestId]);

  const settleAuthorityRequest = useCallback((
    settledRequest: AuthorityRequest,
    status: AuthorityApprovalSettledStatus,
    options?: {
      decision?: AuthorityApprovalDecision;
      resumed?: boolean;
      scheduleClose?: boolean;
    },
  ) => {
    const nextRequest: AuthorityRequest = { ...settledRequest, status };
    locallySettledRequestsRef.current.set(settledRequest.request_id, status);
    setPendingRequests((current) => current.filter((item) => item.request_id !== settledRequest.request_id));

    const settlementKey = `${settledRequest.request_id}:${status}`;
    if (!settlementBroadcastedRef.current.has(settlementKey)) {
      settlementBroadcastedRef.current.add(settlementKey);
      broadcastAuthorityApprovalSettlement({
        requestId: settledRequest.request_id,
        status,
        conversationId: settledRequest.conversation_id,
      });
    }

    if (requestIdRef.current !== settledRequest.request_id) {
      return;
    }

    setRequest(nextRequest);
    setError(null);
    setDecisionState(status === "approved"
      ? { kind: "approved", decision: options?.decision, resumed: Boolean(options?.resumed) }
      : { kind: "rejected" });

    const shouldScheduleClose = options?.scheduleClose
      ?? true;
    if (shouldScheduleClose) {
      scheduleAuthorityApprovalWindowClose(nativeApprovalAvailableRef.current ? "" : approvalReturnToFromLocation());
    }
  }, []);

  const readAuthoritySettlement = useCallback(async (targetRequestId: string): Promise<AuthoritySettlement | null> => {
    const latest = await authorityApprovalResources.getAuthorityRequest(targetRequestId);
    const status = authorityRequestSettledStatus(latest.status);
    return status ? { request: latest, status } : null;
  }, []);

  const readAuthoritySettlementOrNull = useCallback(async (targetRequestId: string): Promise<AuthoritySettlement | null> => {
    try {
      return await readAuthoritySettlement(targetRequestId);
    } catch {
      return null;
    }
  }, [readAuthoritySettlement]);

  const settleFromServer = useCallback(async (targetRequestId: string): Promise<boolean> => {
    const settlement = await readAuthoritySettlementOrNull(targetRequestId);
    if (!settlement) return false;
    settleAuthorityRequest(settlement.request, settlement.status);
    return true;
  }, [readAuthoritySettlementOrNull, settleAuthorityRequest]);

  const tryResumeAfterApproval = useCallback(async (
    settledRequest: AuthorityRequest,
    decision: AuthorityApprovalDecision,
  ): Promise<boolean> => {
    if (!settledRequest.conversation_id) return false;
    const settledApproval = requestToApproval(settledRequest);
    const approvalFollowups = [
      ...(decision.token ? [{
        approval_token: decision.token,
        request_id: settledRequest.request_id,
        permission_id: settledRequest.permission_id,
      }] : []),
      ...((decision.related_approvals ?? [])
        .filter((item) => item.token && item.request_id && item.permission_id)
        .map((item) => ({
          approval_token: item.token,
          request_id: item.request_id,
          permission_id: item.permission_id,
        }))),
    ];
    try {
      await authorityApprovalResources.sendAuthorityResume(
        settledRequest.conversation_id,
        "Internal authority resume.",
        {
          authority_followup: {
            ...(decision.token ? { approval_token: decision.token } : {}),
            request_id: settledRequest.request_id,
            permission_id: settledRequest.permission_id,
            approvals: approvalFollowups,
            hidden: true,
          },
          chat_display: {
            hidden: true,
            reason: "authority_followup",
          },
          runtime_content: authorityApprovalRuntimeContent(settledApproval, decision.token),
        },
      );
      return true;
    } catch (resumeError) {
      console.warn("[authority] approval was saved but resume follow-up failed", resumeError);
      return false;
    }
  }, []);

  useEffect(() => {
    let cancelled = false;

    async function load() {
      if (isAmbientPackApproval) {
        setLoading(false);
        setRequest(null);
        setPendingRequests([]);
        return;
      }
      setLoading(true);
      setError(null);
      try {
        const [single, list] = await Promise.all([
          requestId ? authorityApprovalResources.getAuthorityRequest(requestId) : Promise.resolve(null),
          authorityApprovalResources.listAuthorityRequests({ status: "pending" }),
        ]);
        if (cancelled) return;
        setPendingRequests(list.pending ?? []);
        if (!single) {
          setRequest(null);
          setDecisionState({ kind: "idle" });
          return;
        }

        const singleSettledStatus = authorityRequestSettledStatus(single.status)
          ?? locallySettledRequestsRef.current.get(single.request_id)
          ?? null;
        if (singleSettledStatus) {
          settleAuthorityRequest(single, singleSettledStatus);
          return;
        }

        setRequest(single);
        setDecisionState({ kind: "idle" });
      } catch (loadError) {
        if (cancelled) return;
        setRequest(null);
        setError(loadError instanceof Error ? loadError.message : "承認リクエストを取得できませんでした。");
      } finally {
        if (!cancelled) setLoading(false);
      }
    }

    void load();
    return () => {
      cancelled = true;
    };
  }, [isAmbientPackApproval, requestId, refreshNonce, settleAuthorityRequest]);

  const selectRequest = (nextRequestId: string) => {
    if (!nextRequestId || nextRequestId === requestId) return;
    const nextUrl = new URL(window.location.href);
    nextUrl.searchParams.set("request_id", nextRequestId);
    window.history.replaceState(null, "", `${nextUrl.pathname}${nextUrl.search}${nextUrl.hash}`);
    setAction(null);
    setError(null);
    setDecisionState({ kind: "idle" });
    setRequestId(nextRequestId);
  };

  const refresh = async () => {
    setRefreshNonce((value) => value + 1);
  };

  const finalizeApprovedDecision = useCallback(async (
    settledRequest: AuthorityRequest,
    decision: AuthorityApprovalDecision,
  ) => {
    const serverSettlement = await readAuthoritySettlementOrNull(settledRequest.request_id);
    const finalStatus = serverSettlement?.status ?? "approved";
    const finalRequest: AuthorityRequest = serverSettlement?.request ?? { ...settledRequest, status: finalStatus };
    const resumed = finalStatus === "approved"
      ? await tryResumeAfterApproval(finalRequest, decision)
      : false;
    settleAuthorityRequest(finalRequest, finalStatus, {
      decision: finalStatus === "approved" ? decision : undefined,
      resumed,
    });
  }, [readAuthoritySettlementOrNull, settleAuthorityRequest, tryResumeAfterApproval]);

  const finalizeDeniedRequest = useCallback(async (settledRequest: AuthorityRequest) => {
    const serverSettlement = await readAuthoritySettlementOrNull(settledRequest.request_id);
    const finalStatus = serverSettlement?.status ?? "denied";
    const finalRequest: AuthorityRequest = serverSettlement?.request ?? { ...settledRequest, status: finalStatus };
    settleAuthorityRequest(finalRequest, finalStatus);
  }, [readAuthoritySettlementOrNull, settleAuthorityRequest]);

  const settleDeniedRequest = useCallback((settledRequest: AuthorityRequest) => {
    settleAuthorityRequest({ ...settledRequest, status: "denied" }, "denied");
  }, [settleAuthorityRequest]);

  const getApprovalContext = async (targetRequestId: string) => {
    if (nativeApprovalAvailableRef.current) {
      return getAuthorityApprovalContext(targetRequestId);
    }
    throw new Error("AUTHORITY_BROWSER_TEST_DISABLED");
  };

  const approve = async () => {
    if (!request || !approval) return;
    setAction("approve");
    setError(null);
    const submitApproveOnce = async (): Promise<AuthorityApprovalDecision> => {
      const context = await getApprovalContext(request.request_id);
      const config = authorityApprovalConfig(approval);
      if (typedConfirmationRequired) config.confirmation_text = confirmationText.trim();
      const decision = await authorityApprovalResources.approveAuthorityApproval(request.request_id, {
        scope: selectedScope,
        config,
        related_permissions: authorityRelatedPermissions(approval),
        ui_operator: context.ui_operator,
      });
      if (!decision.approved) throw new Error("authority approval failed");
      return decision;
    };

    try {
      try {
        const decision = await submitApproveOnce();
        await finalizeApprovedDecision(request, decision);
      } catch (postError) {
        if (await settleFromServer(request.request_id)) return;
        if (!authorityApprovalShouldRetryWithFreshContext(postError)) throw postError;
        try {
          const retriedDecision = await submitApproveOnce();
          await finalizeApprovedDecision(request, retriedDecision);
        } catch (retryError) {
          if (await settleFromServer(request.request_id)) return;
          throw retryError;
        }
      }
    } catch (approvalError) {
      setError(authorityApprovalErrorMessage(approvalError));
    } finally {
      setAction(null);
    }
  };

  const reject = async () => {
    if (!request) return;
    setAction("reject");
    setError(null);
    const submitRejectOnce = async (): Promise<void> => {
      const context = await getApprovalContext(request.request_id);
      await authorityApprovalResources.denyAuthorityApproval(request.request_id, {
        reason: "Denied from dedicated authority approval window",
        persist: false,
        ui_operator: context.ui_operator,
      });
    };

    try {
      try {
        await submitRejectOnce();
        settleDeniedRequest(request);
        await finalizeDeniedRequest(request);
      } catch (postError) {
        if (await settleFromServer(request.request_id)) return;
        if (!authorityApprovalShouldRetryWithFreshContext(postError)) throw postError;
        try {
          await submitRejectOnce();
          settleDeniedRequest(request);
          await finalizeDeniedRequest(request);
        } catch (retryError) {
          if (await settleFromServer(request.request_id)) return;
          throw retryError;
        }
      }
    } catch (rejectionError) {
      setError(authorityApprovalErrorMessage(rejectionError));
    } finally {
      setAction(null);
    }
  };

  if (isAmbientPackApproval) {
    return <AmbientPackAuthorityApprovalWindow />;
  }

  return (
    <main className="min-h-screen bg-[#09090b] text-zinc-100">
      <div className="mx-auto flex min-h-screen w-full max-w-3xl flex-col px-5 py-5">
        <header className="flex items-start justify-between gap-4 border-b border-zinc-800 pb-4">
          <div className="min-w-0">
            <div className="flex items-center gap-2 text-[11px] font-medium text-amber-200">
              <ShieldAlert size={14} />
              Tobkiriの許可
            </div>
            <h1 className="mt-2 break-words text-xl font-semibold text-zinc-50">{title}</h1>
            <p className="mt-1 text-xs leading-5 text-zinc-500">{request?.display_metadata?.summary || request?.reason || "pending request"}</p>
          </div>
          <button
            type="button"
            onClick={() => void refresh()}
            className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg border border-zinc-800 text-zinc-400 hover:border-zinc-700 hover:text-zinc-100"
            title="再読み込み"
          >
            <RefreshCw size={15} />
          </button>
        </header>

        {error && (
          <div className="mt-4 rounded-lg border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-100">
            {error}
          </div>
        )}

        {displayedSettledStatus && (
          <div className={cn(
            "mt-4 rounded-lg border px-3 py-3 text-sm",
            displayedSettledStatus === "approved"
              ? "border-emerald-500/30 bg-emerald-500/10 text-emerald-100"
              : "border-zinc-700 bg-zinc-900 text-zinc-200",
          )}>
            <div className="flex items-center gap-2 font-medium">
              {displayedSettledStatus === "approved" ? <ShieldCheck size={16} /> : <ShieldX size={16} />}
              {authorityApprovalSettledLabel(displayedSettledStatus)}
            </div>
            {decisionState.kind === "approved" && decisionState.decision ? (
              <p className="mt-1 text-xs text-emerald-200/80">
                許可範囲: {SCOPE_LABELS[decisionState.decision.scope] ?? decisionState.decision.scope}{decisionState.resumed ? " / 続行しました" : ""}
              </p>
            ) : (
              <p className={cn(
                "mt-1 text-xs",
                displayedSettledStatus === "approved" ? "text-emerald-200/80" : "text-zinc-400",
              )}>
                {nativeApprovalAvailable ? "このリクエストは処理済みです。ウィンドウを閉じます。" : "このリクエストは処理済みです。追加の操作は不要です。"}
              </p>
            )}
          </div>
        )}

        <section className="mt-5 grid gap-4">
          {loading ? (
            <div className="flex min-h-56 items-center justify-center rounded-lg border border-zinc-800 bg-zinc-950">
              <Loader2 className="animate-spin text-zinc-500" size={22} />
            </div>
          ) : request && approval ? (
            <>
              <ApprovalDecisionSurface
                approval={{
                  ...authorityApprovalViewModel(approval, authorityApprovalTitle(approval)),
                  status: displayedSettledStatus ?? (action === "approve" ? "approving" : action === "reject" ? "denying" : "pending"),
                  trustedWindowRequired: false,
                  scope: allowedScopes.map((scope) => SCOPE_LABELS[scope]).join(" / "),
                  persistence: "下の選択内容とサーバーが発行する期限に従います。",
                  auditText: request.display_metadata?.audit_text || "この許可操作だけをローカルに記録します。",
                }}
              />
              <div className="rounded-lg border border-zinc-800 bg-zinc-950 p-4">
                <div className="flex flex-wrap items-center gap-2">
                  <span className={cn(
                    "rounded border px-2 py-1 text-[11px] font-medium",
                    authorityApprovalRiskTone(request.risk_level),
                  )}>
                    {request.risk_level || "authority"}
                  </span>
                  <span className="rounded border border-zinc-800 px-2 py-1 text-[11px] text-zinc-400" title="詳しい権限ID">
                    {request.permission_id}
                  </span>
                  <span className="rounded border border-zinc-800 px-2 py-1 text-[11px] text-zinc-400">
                    {request.status}
                  </span>
                </div>

                <dl className="mt-4 grid gap-3 text-xs sm:grid-cols-2">
                  {detailRows.map((row) => (
                    <div key={row.label} className={["接続先", "操作内容", "対象path", "対象URL"].includes(row.label) ? "sm:col-span-2" : undefined}>
                      <dt className="text-zinc-600">{row.label}</dt>
                      <dd className="mt-1 whitespace-pre-wrap break-words text-zinc-200">{row.value}</dd>
                    </div>
                  ))}
                </dl>

                {showApprovalControls && typedConfirmationRequired && (
                  <div className="mt-4 rounded-lg border border-red-500/35 bg-red-500/10 p-3">
                    <p className="text-[11px] font-semibold text-red-200">重要な確認</p>
                    <p className="mt-2 text-xs leading-5 text-red-100/85">
                      この操作はTobkiriの外側に触れる可能性があります。続けるには次の文字をそのまま入力してください。
                    </p>
                    <code className="mt-2 block rounded-md border border-red-400/25 bg-black/35 px-2 py-1.5 font-mono text-xs text-red-100">
                      {confirmationPhrase || "confirmation phrase missing"}
                    </code>
                    <input
                      value={confirmationText}
                      onChange={(event) => setConfirmationText(event.currentTarget.value)}
                      disabled={controlsDisabled}
                      spellCheck={false}
                      autoComplete="off"
                      className="mt-2 h-9 w-full rounded-md border border-red-400/30 bg-black/35 px-2 font-mono text-xs text-red-50 outline-none placeholder:text-red-100/30 focus:border-red-200"
                      placeholder="上の文字を入力"
                    />
                  </div>
                )}

                {showApprovalControls && (
                  <div className="mt-4 rounded-lg border border-zinc-800 bg-black/30 p-3">
                    <p className="text-[11px] font-medium text-zinc-400">許可する範囲</p>
                    <div className="mt-2 grid grid-cols-2 gap-2">
                      {allowedScopes.map((scope) => (
                        <button
                          key={scope}
                          type="button"
                          disabled={controlsDisabled}
                          onClick={() => setSelectedScope(scope)}
                          className={cn(
                            "flex h-9 items-center justify-center rounded-lg border text-xs font-medium transition-colors disabled:opacity-50",
                            selectedScope === scope
                              ? "border-zinc-100 bg-zinc-100 text-zinc-950"
                              : "border-zinc-800 bg-zinc-950 text-zinc-400 hover:border-zinc-700 hover:text-zinc-100",
                          )}
                        >
                          {SCOPE_LABELS[scope]}
                        </button>
                      ))}
                    </div>
                  </div>
                )}

                <div className="mt-4 rounded-lg border border-zinc-800 bg-black/30 p-3">
                  <p className="text-[11px] font-medium text-zinc-400">記録される内容</p>
                  <p className="mt-1 text-xs leading-5 text-zinc-500">
                    {request.display_metadata?.audit_text || "この許可操作だけをローカルに記録します。"}
                  </p>
                </div>

                <details className="mt-4 text-xs text-zinc-500">
                  <summary className="cursor-pointer select-none hover:text-zinc-300">詳しい内容</summary>
                  <pre className="mt-2 max-h-48 overflow-auto whitespace-pre-wrap rounded-lg border border-zinc-800 bg-black/40 p-3 font-mono text-[11px]">
                    {stableJson(request.resource)}
                  </pre>
                </details>
              </div>

              {showApprovalControls ? (
                <div className="flex items-center justify-end gap-2">
                  <button
                    type="button"
                    onClick={() => void reject()}
                    disabled={controlsDisabled}
                    className="flex h-10 min-w-28 items-center justify-center gap-2 rounded-lg border border-zinc-800 px-4 text-sm font-semibold text-zinc-300 hover:border-red-500/40 hover:bg-red-500/10 hover:text-red-100 disabled:opacity-50"
                  >
                    {action === "reject" ? <Loader2 className="animate-spin" size={15} /> : <X size={15} />}
                    拒否{ambientEnabled ? " (2)" : ""}
                  </button>
                  <button
                    type="button"
                    onClick={() => void approve()}
                    disabled={controlsDisabled || !typedConfirmationSatisfied}
                    className="flex h-10 min-w-32 items-center justify-center gap-2 rounded-lg bg-zinc-100 px-4 text-sm font-semibold text-zinc-950 hover:bg-white disabled:opacity-50"
                  >
                    {action === "approve" ? <Loader2 className="animate-spin" size={15} /> : <Check size={15} />}
                    承認{ambientEnabled ? " (3)" : ""}
                  </button>
                </div>
              ) : !displayedSettledStatus ? (
                <div className="rounded-lg border border-zinc-800 bg-zinc-950 px-3 py-2 text-sm text-zinc-400">
                  {request.status === "pending" && !approvalContextAvailable
                    ? "承認操作は Tobkiri Launcher の専用ウィンドウで実行してください。"
                    : `このリクエストは ${request.status} のため、この画面では操作できません。`}
                </div>
              ) : null}
            </>
          ) : (
            <div className="rounded-lg border border-zinc-800 bg-zinc-950 p-4 text-sm text-zinc-400">
              request_id が見つかりません。
            </div>
          )}
        </section>

        {showPendingRequestPicker && (
          <section className="mt-5 border-t border-zinc-800 pt-4">
            <p className="text-[11px] font-medium uppercase tracking-[0.16em] text-zinc-600">Pending</p>
            <div className="mt-2 grid gap-2">
              {pendingRequests.map((item) => (
                <button
                  key={item.request_id}
                  type="button"
                  onClick={() => selectRequest(item.request_id)}
                  className={cn(
                    "flex min-w-0 items-center justify-between gap-3 rounded-lg border px-3 py-2 text-left text-xs",
                    item.request_id === requestId
                      ? "border-sky-500/40 bg-sky-500/10 text-sky-100"
                      : "border-zinc-800 bg-zinc-950 text-zinc-400 hover:border-zinc-700 hover:text-zinc-100",
                  )}
                >
                  <span className="min-w-0 truncate">{windowTitle(item)}</span>
                  <span className="shrink-0 text-[10px] text-zinc-600">{item.risk_level}</span>
                </button>
              ))}
            </div>
          </section>
        )}
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
  const rumiPermissionCount = grantedPermissionCount(status, AMBIENT_REQUIRED_PERMISSIONS, "rumi");
  const osPermissionCount = grantedPermissionCount(status, AMBIENT_OS_PERMISSIONS, "os");
  const rumiReady = hasAllRumiPermissions(status);

  const reloadStatus = async () => {
    setLoading(true);
    setError(null);
    try {
      setStatus(await ambientTriggerClient.status());
    } catch (loadError) {
      setError(loadError instanceof Error ? loadError.message : "指で録音の状態を取得できませんでした。");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    document.title = "Tobkiriの許可";
    void reloadStatus();
  }, []);

  useEffect(() => {
    rumiReadyRef.current = rumiReady;
  }, [rumiReady]);

  const broadcastAmbientApprovalCancelled = useCallback(() => {
    if (settlementBroadcastedRef.current || closingAfterApprovalRef.current || rumiReadyRef.current) return;
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
    if (loading || !rumiReady) return;
    finishAmbientApproval(status);
  }, [finishAmbientApproval, loading, rumiReady, status]);

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
    try {
      if (await closeCurrentWindow()) return;
    } catch {
      // Fall back to the browser close path below.
    }
    window.close();
    window.setTimeout(() => setAction(null), 300);
  };

  const openAmbientWindow = async () => {
    setAction("open");
    setError(null);
    try {
      const opened = await openFingerRecordingWindow();
      if (!opened) {
        setMessage("Tobkiri Launcherから開くと、指で録音は別ウィンドウで表示されます。");
      }
    } catch (openError) {
      console.info("[ambient] ambient trigger window unavailable", openError);
      setMessage("Tobkiri Launcherから開くと、指で録音は別ウィンドウで表示されます。");
    } finally {
      window.setTimeout(() => setAction(null), 300);
    }
  };

  return (
    <main className="min-h-screen bg-[#09090b] text-zinc-100">
      <div className="mx-auto flex min-h-screen w-full max-w-[520px] flex-col">
        <header className="flex items-start justify-between gap-2 border-b border-zinc-800 px-3 py-2">
          <div className="min-w-0">
            <div className="flex items-center gap-2 text-[10px] font-medium text-amber-200">
              <ShieldAlert size={14} />
              Rumi内の許可
            </div>
            <h1 className="mt-1 break-words text-base font-semibold leading-5 text-zinc-50">Tobkiriの許可</h1>
            <p className="mt-0.5 text-[11px] leading-4 text-zinc-400">
              指で録音をRumi内で使えるようにします。Macのマイク/カメラ許可は別に確認します。
            </p>
          </div>
          <div className="flex shrink-0 items-center gap-1.5">
            <AmbientApprovalPrivacyToggle />
            <button
              type="button"
              onClick={() => void reloadStatus()}
              className="flex h-8 w-8 items-center justify-center rounded-md border border-zinc-800 text-zinc-400 hover:border-zinc-700 hover:text-zinc-100"
              title="再読み込み"
            >
              <RefreshCw size={14} />
            </button>
          </div>
        </header>

        {error && (
          <div className="border-b border-red-500/25 bg-red-500/10 px-3 py-2 text-xs text-red-100">
            {error}
          </div>
        )}

        {message && (
          <div className="border-b border-emerald-500/25 bg-emerald-500/10 px-3 py-2 text-xs text-emerald-100">
            <div className="flex items-center gap-2 font-medium">
              <ShieldCheck size={16} />
              {message}
            </div>
          </div>
        )}

        <section className="min-h-0 flex-1">
          <div className="border-b border-zinc-800 bg-zinc-950">
            <div className="flex flex-wrap items-center gap-1.5 px-3 py-2">
              {!rumiReady && (
                <span className="rounded border border-sky-500/30 bg-sky-500/10 px-1.5 py-0.5 text-[10px] font-medium text-sky-100">
                  許可が必要
                </span>
              )}
              {rumiReady && (
                <span className="rounded border border-emerald-500/30 bg-emerald-500/10 px-1.5 py-0.5 text-[10px] font-medium text-emerald-100">
                  承認済み
                </span>
              )}
              <span className="rounded border border-zinc-800 px-1.5 py-0.5 text-[10px] text-zinc-400">
                Rumi {rumiPermissionCount}/{AMBIENT_REQUIRED_PERMISSIONS.length}
              </span>
              <span className="rounded border border-zinc-800 px-1.5 py-0.5 text-[10px] text-zinc-400">
                OS {osPermissionCount}/{AMBIENT_OS_PERMISSIONS.length}
              </span>
            </div>

            {loading ? (
              <div className="flex min-h-28 items-center justify-center border-t border-zinc-800 bg-black/20">
                <Loader2 className="animate-spin text-zinc-500" size={22} />
              </div>
            ) : (
              <div className="grid">
                {rumiReady && (
                  <section className="border-t border-emerald-500/20 bg-emerald-500/10 px-3 py-3 text-xs leading-5 text-emerald-50">
                    <p className="font-medium">承認済みです</p>
                    <p className="mt-0.5 text-emerald-50/80">この画面を閉じて、指で録音へ戻ります。</p>
                  </section>
                )}

                {!rumiReady && (
                  <>
                    <section className="border-t border-zinc-800 px-3 py-2">
                      <p className="text-[10px] font-semibold text-sky-200">Rumiが受け取る入口</p>
                      <div className="mt-1.5 grid gap-1.5">
                        {ambientApprovalPermissionRows.map((row) => (
                          <AmbientApprovalRow key={row.permissionId} {...row} granted={Boolean(status?.permissions.rumi[row.permissionId]?.granted)} />
                        ))}
                      </div>
                    </section>

                    <section className="border-t border-zinc-800 bg-black/20 px-3 py-2 text-[11px] leading-4 text-zinc-400">
                      <span className="font-medium text-zinc-200">OS許可は別。</span>
                      <span> ここではRumi内の許可だけ保存します。実際のマイク・カメラ確認は、指で録音画面で出ます。</span>
                    </section>
                  </>
                )}
              </div>
            )}
          </div>

          <div className="flex items-center justify-end gap-2 px-3 py-2">
            <button
              type="button"
              onClick={() => void closeWindow()}
              disabled={action !== null}
              className="flex h-9 min-w-24 items-center justify-center gap-2 rounded-md border border-zinc-800 px-3 text-sm font-semibold text-zinc-300 hover:border-zinc-700 hover:text-zinc-100 disabled:opacity-50"
            >
              {action === "close" ? <Loader2 className="animate-spin" size={15} /> : <X size={15} />}
              {rumiReady ? "閉じる" : "あとで"}
            </button>
            {rumiReady && (
              <button
                type="button"
                onClick={() => void openAmbientWindow()}
                disabled={loading || action !== null}
                className="flex h-9 min-w-36 items-center justify-center gap-2 rounded-md bg-emerald-200 px-3 text-sm font-semibold text-zinc-950 hover:bg-emerald-100 disabled:opacity-50"
              >
                {action === "open" ? <Loader2 className="animate-spin" size={15} /> : <ExternalLink size={15} />}
                指で録音を開く
              </button>
            )}
            {!rumiReady && (
              <button
                type="button"
                onClick={() => void approve()}
                disabled={loading || action !== null}
                className="flex h-9 min-w-32 items-center justify-center gap-2 rounded-md bg-sky-300 px-3 text-sm font-semibold text-zinc-950 hover:bg-sky-200 disabled:opacity-50"
              >
                {action === "approve" ? <Loader2 className="animate-spin" size={15} /> : <Check size={15} />}
                許可する
              </button>
            )}
          </div>
        </section>
      </div>
    </main>
  );
}

const ambientApprovalPermissionRows = [
  {
    permissionId: AMBIENT_MIC_PERMISSION,
    label: "マイク入力",
    detail: "OKマークで録音している間に使う実入力",
    badge: "実入力",
    className: "border-rose-400/40 text-rose-100",
    dotClassName: "bg-rose-300",
    badgeClassName: "text-rose-200/80",
  },
  {
    permissionId: AMBIENT_CAMERA_PERMISSION,
    label: "カメラで手を見る",
    detail: "映像を保存せず、手の点だけをその場で判定",
    badge: "実入力",
    className: "border-cyan-400/40 text-cyan-100",
    dotClassName: "bg-cyan-300",
    badgeClassName: "text-cyan-200/80",
  },
  {
    permissionId: "ambient.trigger.dispatch",
    label: "音声をAIに送る",
    detail: "OKマークを開いた後、録音を入力としてdispatch",
    badge: "送信",
    className: "border-violet-400/40 text-violet-100",
    dotClassName: "bg-violet-300",
    badgeClassName: "text-violet-200/80",
  },
] satisfies Array<AmbientApprovalRowProps & { permissionId: string }>;

type AmbientApprovalRowProps = {
  label: string;
  detail: string;
  badge: string;
  className: string;
  dotClassName: string;
  badgeClassName: string;
  granted?: boolean;
};

function AmbientApprovalRow({
  label,
  detail,
  badge,
  className,
  dotClassName,
  badgeClassName,
  granted,
}: AmbientApprovalRowProps) {
  return (
    <div className={cn("flex min-w-0 items-center gap-2 border-l pl-2 py-1 text-xs", className)}>
      <span className={cn("h-2 w-2 shrink-0 rounded-full", dotClassName)} />
      <div className="min-w-0 flex-1">
        <div className="flex min-w-0 items-baseline gap-2">
          <span className="truncate font-semibold">{label}</span>
          <span className={cn("shrink-0 text-[10px]", badgeClassName)}>{badge}</span>
        </div>
        <p className="mt-0.5 truncate text-[11px] opacity-75">{detail}</p>
      </div>
      {granted && <Check size={13} className="shrink-0 text-emerald-200" />}
    </div>
  );
}

function AmbientApprovalPrivacyToggle() {
  const [open, setOpen] = useState(false);

  return (
    <div className="relative">
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        className={cn(
          "flex h-8 w-8 items-center justify-center rounded-md border text-sm font-semibold",
          open
            ? "border-emerald-400/35 bg-emerald-500/10 text-emerald-100"
            : "border-zinc-800 text-zinc-400 hover:border-zinc-700 hover:text-zinc-100",
        )}
        aria-label="プライバシー"
        title="プライバシー"
      >
        i
      </button>
      {open && (
        <div className="absolute right-0 top-9 rumi-layer-local-popover w-64 rounded-lg border border-emerald-400/25 bg-zinc-950 p-3 text-xs leading-5 text-emerald-50 shadow-2xl shadow-black/50">
          <p className="font-medium">音声・画像・カメラ映像は保存しません。</p>
          <p className="mt-1 text-emerald-50/80">履歴には、指録音を使った時刻と結果だけを残します。</p>
        </div>
      )}
    </div>
  );
}
