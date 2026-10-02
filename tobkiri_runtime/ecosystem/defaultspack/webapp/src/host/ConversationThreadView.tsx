import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ComposerRenderer } from "../renderers/ComposerRenderer";
import { ChatMessagesRenderer } from "../renderers/ChatMessagesRenderer";
import { chatMessageToUiMessage } from "../lib/chatUiMessage";
import { orderConversationMessages } from "../lib/chat";
import { createWidgetConversationContext } from "../lib/widgetContext";
import {
  viewContextKey, viewOperationRequest, viewReadRequest,
  type RegisteredCatalogView, type ViewInputContext, type ViewNavigationGuardChange,
} from "./catalogViewRegistry";
import type { FrontendCapabilityInvoker, FrontendCatalog } from "./frontendContracts";
import {
  mergeThreadTicketTurn, newestThreadTurn, readConversationThread, readThreadEventTurn, threadRequestPayload,
  threadTurnFailureNotice, threadTurnIsActive, threadTurnIsSaved,
  type ThreadSnapshot, type ThreadTicket,
} from "./conversationThreadState";
import { viewOperationOutcome } from "./viewControlState";
import { mergeThreadProgress, narrowThreadProgressDeadline, threadProgressPayload, threadProgressReadCapture, threadProgressWitness, type ThreadProgressState } from "./threadProgressState";

/** A text thread with server-owned context and exact captured public operations. */
export function ConversationThreadView({
  registered, catalog, capabilities, snapshot, sourceReady, onRefresh,
  onDirtyChange, context = {},
}: {
  registered: RegisteredCatalogView; catalog: FrontendCatalog;
  capabilities: FrontendCapabilityInvoker; snapshot: unknown; sourceReady: boolean;
  onRefresh: () => void; onDirtyChange?: ViewNavigationGuardChange; context?: ViewInputContext;
}) {
  const descriptor = registered.view.conversation_thread;
  const parsed = useMemo(() => descriptor ? readConversationThread(snapshot, descriptor) : null, [snapshot, descriptor]);
  const lastConfirmed = useRef<ThreadSnapshot | null>(null);
  if (parsed) lastConfirmed.current = parsed;
  const thread = parsed ?? lastConfirmed.current;
  const [draft, setDraft] = useState("");
  const [ticket, setTicket] = useState<ThreadTicket | null>(null);
  const [progress, setProgress] = useState<ThreadProgressState | null>(null);
  const [progressStatus, setProgressStatus] = useState<"waiting" | "unavailable" | "rejected">("waiting");
  const [busy, setBusy] = useState(false);
  const [recovering, setRecovering] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [feedback, setFeedback] = useState<string | null>(null);
  const active = useRef(true);
  const pending = useRef(false);
  const reading = useRef(false);
  const readingProgress = useRef(false);
  const progressRef = useRef(progress);
  const progressCaptureDeadline = useRef<number | null | undefined>(undefined);
  const stopping = useRef(false);
  const reconciling = useRef(false);
  const state = useRef({ draft, ticket, activeTurnId: thread?.turn?.id });
  state.current = { draft, ticket, activeTurnId: ticket?.turnId ?? thread?.turn?.id };
  const endRef = useRef<HTMLDivElement | null>(null);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const ownerId = JSON.stringify([registered.reference, "conversation_thread", viewContextKey(registered, context)]);
  const live = useRef({ parsed, sourceReady, ownerId, catalog, registered });
  live.current = { parsed, sourceReady, ownerId, catalog, registered };
  const canLeave = useCallback(() => {
    const current = state.current;
    if (pending.current || reconciling.current || current.ticket) return false;
    return !current.draft || window.confirm("Discard this unsent message?");
  }, []);
  useEffect(() => {
    active.current = true;
    onDirtyChange?.(ownerId, canLeave);
    return () => { active.current = false; onDirtyChange?.(ownerId, null); };
  }, [onDirtyChange, ownerId, canLeave]);
  useEffect(() => {
    const guard = (event: BeforeUnloadEvent) => {
      if (!pending.current && !reconciling.current && !state.current.ticket && !state.current.draft) return;
      event.preventDefault(); event.returnValue = "";
    };
    window.addEventListener("beforeunload", guard);
    return () => window.removeEventListener("beforeunload", guard);
  }, []);

  // A response alone is insufficient: the canonical child must also agree.
  useEffect(() => {
    if (!ticket || !thread || thread.conversation.id !== ticket.conversationId) return;
    if (ticket.contradictoryRevision !== undefined) {
      const fresh = thread.turn;
      if (fresh?.id === ticket.turnId && fresh.revision > ticket.contradictoryRevision) {
        setTicket((current) => current?.turnId === ticket.turnId ? mergeThreadTicketTurn(current, fresh) : current);
      }
      return;
    }
    const turn = newestThreadTurn(thread.turn?.id === ticket.turnId ? thread.turn : null, ticket.turn);
    if (!turn) return;
    const failure = threadTurnFailureNotice(turn);
    if (failure) {
      setError(failure); setFeedback(null); setTicket(null); return;
    }
    if (!threadTurnIsSaved(turn, thread, ticket.turnId)) return;
    setDraft((current) => current === ticket.draft ? "" : current);
    setTicket(null); setFeedback(null); setError(null);
  }, [ticket, thread]);

  const readProgress = useCallback(async () => {
    const current = state.current.ticket;
    const fresh = live.current;
    if (!current || !descriptor?.progress || readingProgress.current) return;
    const witness = fresh.sourceReady && fresh.parsed ? threadProgressWitness(current, fresh.parsed) : null;
    if (!witness) { setProgress(null); setProgressStatus("waiting"); return; }
    const before = progressRef.current;
    if (before && before.expiresAtMs <= Date.now()) { setProgress(null); setProgressStatus("unavailable"); return; }
    const payload = threadProgressPayload(descriptor.progress, current, context, before?.cursor ?? 0);
    const captured = payload ? threadProgressReadCapture(catalog, registered, payload) : null;
    if (!captured) { setProgress(null); setProgressStatus("unavailable"); return; }
    progressCaptureDeadline.current = narrowThreadProgressDeadline(progressCaptureDeadline.current, captured.expiresAtMs);
    if (progressCaptureDeadline.current !== null && progressCaptureDeadline.current <= Date.now()) {
      setProgress(null); setProgressStatus("unavailable"); return;
    }
    const capture = fresh.ownerId;
    readingProgress.current = true;
    try {
      const page = await capabilities.readDataSource(captured.request);
      const latest = live.current;
      const retained = state.current.ticket;
      const latestWitness = retained && latest.sourceReady && latest.parsed
        ? threadProgressWitness(retained, latest.parsed) : null;
      if (!active.current || latest.ownerId !== capture || retained?.turnId !== current.turnId) return;
      const latestCapture = threadProgressReadCapture(latest.catalog, latest.registered, payload!);
      const deadline = latestCapture ? narrowThreadProgressDeadline(progressCaptureDeadline.current, latestCapture.expiresAtMs) : null;
      if (!latestWitness || !latestCapture || deadline !== null && deadline <= Date.now()) {
        setProgress(null); setProgressStatus("unavailable"); return;
      }
      progressCaptureDeadline.current = deadline;
      const merged = mergeThreadProgress(before, page, latestWitness);
      if (!merged) { setProgress(null); setProgressStatus("rejected"); return; }
      progressRef.current = merged;
      setProgress(merged); setProgressStatus("waiting");
    } catch {
      if (active.current && state.current.ticket?.turnId === current.turnId && live.current.ownerId === capture) {
        setProgress(null); setProgressStatus("unavailable");
      }
    } finally { readingProgress.current = false; }
  }, [descriptor, catalog, registered, capabilities, context]);

  const readProgressRef = useRef(readProgress);
  readProgressRef.current = readProgress;
  useEffect(() => {
    if (!ticket) { setProgress(null); progressRef.current = null; progressCaptureDeadline.current = undefined; setProgressStatus("waiting"); return; }
    void readProgressRef.current();
  }, [ticket?.turnId, parsed, sourceReady]);
  useEffect(() => {
    if (!progress) return;
    const remaining = Math.min(progress.expiresAtMs, progressCaptureDeadline.current ?? progress.expiresAtMs) - Date.now();
    const expire = () => { setProgress(null); setProgressStatus("unavailable"); };
    if (remaining <= 0) { expire(); return; }
    const timer = window.setTimeout(expire, remaining + 1);
    return () => window.clearTimeout(timer);
  }, [progress]);

  const readEvents = useCallback(async () => {
    const current = state.current.ticket;
    if (!current || !descriptor?.events || reading.current) { onRefresh(); return; }
    const payload = threadRequestPayload(descriptor.events, current.source, context, current.turnId);
    const request = payload ? viewReadRequest(catalog, registered, descriptor.events.operation, payload) : null;
    if (!request) { setError("この実行を現在の Profile で照合できません。自動再送はしません。"); return; }
    reading.current = true;
    try {
      const result = await capabilities.readDataSource(request);
      if (!active.current) return;
      const turn = readThreadEventTurn(result, current.conversationId, current.turnId);
      if (!turn) { setError("実行記録の識別情報を照合できません。入力内容と送信IDを保持しています。"); return; }
      setTicket((latest) => latest?.turnId === current.turnId ? mergeThreadTicketTurn(latest, turn) : latest);
      onRefresh();
    } catch {
      if (active.current) setError("実行記録を取得できません。入力内容と送信IDを保持し、自動再送しません。");
    } finally { reading.current = false; }
  }, [descriptor, catalog, registered, capabilities, context, onRefresh]);

  const readEventsRef = useRef(readEvents);
  readEventsRef.current = readEvents;
  const running = Boolean(ticket || busy || threadTurnIsActive(thread?.turn ?? null));
  useEffect(() => {
    if (!running) return;
    const poll = () => {
      if (document.visibilityState === "hidden") return;
      if (state.current.ticket) void readEventsRef.current(); else onRefresh();
      if (state.current.ticket) void readProgressRef.current();
    };
    const timer = window.setInterval(poll, 2000);
    document.addEventListener("visibilitychange", poll);
    return () => { window.clearInterval(timer); document.removeEventListener("visibilitychange", poll); };
  }, [running, onRefresh]);

  const send = async () => {
    if (!descriptor || !parsed || !sourceReady || pending.current || state.current.ticket
      || threadTurnIsActive(parsed.turn) || !draft.trim()) return;
    if (typeof globalThis.crypto?.randomUUID !== "function") {
      setError("送信IDを安全に生成できません。入力内容は保持しました。"); return;
    }
    const turnId = globalThis.crypto.randomUUID();
    const payload = threadRequestPayload(descriptor.send, snapshot, context, turnId, draft);
    const request = payload ? viewOperationRequest(catalog, registered, descriptor.send.operation, payload) : null;
    if (!request) { setError("この送信操作は現在の Profile で利用できません。"); return; }
    const captured: ThreadTicket = { turnId, conversationId: parsed.conversation.id, draft, source: JSON.parse(JSON.stringify(snapshot)) as unknown, turn: null };
    state.current.ticket = captured;
    progressRef.current = null; progressCaptureDeadline.current = undefined; setProgress(null); setProgressStatus("waiting");
    pending.current = true; setTicket(captured); setBusy(true); setError(null); setFeedback(null);
    try {
      const result = await capabilities.invokeAction(request);
      if (!active.current) return;
      const outcome = viewOperationOutcome(result);
      const turn = readThreadEventTurn(result, captured.conversationId, captured.turnId);
      if (turn) setTicket((current) => current?.turnId === turnId ? mergeThreadTicketTurn(current, turn) : current);
      else setFeedback(outcome === "approval"
        ? "Tobkiri の承認画面で確認を待っています。入力内容を保持しています。"
        : "送信結果の照合を待っています。自動再送はしません。");
      onRefresh();
    } catch {
      if (active.current) setError("送信結果を確認できません。入力内容と送信IDを保持し、自動再送しません。");
    } finally {
      pending.current = false;
      if (active.current) setBusy(false);
    }
  };

  const turnId = ticket?.turnId ?? (threadTurnIsActive(thread?.turn ?? null) ? thread?.turn?.id : undefined);
  const stopSource = ticket?.source ?? snapshot;
  const stopPayload = descriptor?.stop && turnId ? threadRequestPayload(descriptor.stop, stopSource, context, turnId) : null;
  const stopRequest = descriptor?.stop && stopPayload ? viewOperationRequest(catalog, registered, descriptor.stop.operation, stopPayload) : null;
  const stop = async () => {
    if (!stopRequest || !turnId || stopping.current) return;
    stopping.current = true;
    const capturedTurnId = turnId;
    setFeedback("停止を要求しています。実行結果の照合を続けます。");
    try {
      await capabilities.invokeAction(stopRequest);
      if (active.current && state.current.activeTurnId === capturedTurnId) { onRefresh(); void readEvents(); }
    } catch {
      if (active.current && state.current.activeTurnId === capturedTurnId) setError("停止要求の結果を確認できません。実行結果を照合してください。");
    } finally { stopping.current = false; }
  };

  const recoveryPayload = descriptor?.reconcile && ticket
    ? threadRequestPayload(descriptor.reconcile, ticket.source, context, ticket.turnId) : null;
  const recoveryRequest = descriptor?.reconcile && recoveryPayload
    ? viewOperationRequest(catalog, registered, descriptor.reconcile.operation, recoveryPayload) : null;
  const reconcile = async () => {
    const captured = state.current.ticket;
    if (!captured || !descriptor?.reconcile || pending.current || reconciling.current) return;
    const payload = threadRequestPayload(descriptor.reconcile, captured.source, context, captured.turnId);
    const request = payload ? viewOperationRequest(catalog, registered, descriptor.reconcile.operation, payload) : null;
    if (!request) return;
    reconciling.current = true;
    setRecovering(true); setError(null); setFeedback(null);
    try {
      const result = await capabilities.invokeAction(request);
      if (!active.current || state.current.ticket?.turnId !== captured.turnId) return;
      const outcome = viewOperationOutcome(result);
      if (outcome === "failed") throw new Error("recovery_failed");
      const turn = readThreadEventTurn(result, captured.conversationId, captured.turnId);
      if (turn) setTicket((current) => current?.turnId === captured.turnId
        ? mergeThreadTicketTurn(current, turn) : current);
      setFeedback(outcome === "approval"
        ? "Tobkiri の承認画面で確認を待っています。入力内容と送信IDを保持しています。"
        : "復旧操作が戻りました。保存された実行記録と会話の一致を確認しています。");
      onRefresh();
    } catch {
      if (active.current && state.current.ticket?.turnId === captured.turnId) {
        setError("復旧結果を確認できません。入力内容と送信IDを保持し、自動再送しません。");
      }
    } finally {
      reconciling.current = false;
      if (active.current) setRecovering(false);
    }
  };

  if (!descriptor || !thread) return <p role="status">この会話はまだ利用できません。利用可能な操作で会話を作成するか、再読み込みしてください。</p>;
  const messages = orderConversationMessages(thread.messages).map((message) => chatMessageToUiMessage(message));
  const start = thread.turn?.started_at_ms;
  const currentWitness = ticket && sourceReady && parsed ? threadProgressWitness(ticket, parsed) : null;
  const visibleProgress = progress && currentWitness && progress.expiresAtMs > Date.now()
    && (progressCaptureDeadline.current == null || progressCaptureDeadline.current > Date.now())
    && progress.binding.turn_id === currentWitness.turnId && progress.binding.conversation_id === currentWitness.conversationId
    && progress.binding.parent_id === currentWitness.parentId && progress.binding.conversation_revision === currentWitness.conversationRevision
    && progress.binding.input_digest === currentWitness.inputDigest ? progress : null;
  return <div data-tobkiri-conversation-thread className="flex min-h-0 min-w-0 w-full flex-col gap-2">
    {thread.modelLabel && <p className="break-words text-xs text-zinc-400">モデル: {thread.modelLabel}</p>}
    {!parsed && <p role="alert">最新の会話を照合できません。前回確認した内容を表示しています。</p>}
    {feedback && <p role="status" className="text-sm">{feedback}</p>}
    {ticket?.contradictoryRevision !== undefined && <p role="alert">実行記録に矛盾があります。入力内容を保持し、更新された記録を待っています。</p>}
    <div className="flex h-[min(52vh,480px)] min-h-48 min-w-0 flex-col overflow-hidden rounded-lg border border-zinc-800">
      <ChatMessagesRenderer error={error} isMessagesRegionVisible isLoading={false}
        isNewConversation={false} isGenerating={running}
        pendingStatus={busy ? "送信結果を待っています" : "実行状態を照合しています"}
        pendingStartedAt={typeof start === "number" && Number.isSafeInteger(start) && start >= 0 ? start : null}
        messages={messages} messagesEndRef={endRef} messagesScrollRef={scrollRef}
        unknownBlockStrategy="placeholder" showActivityInMessages showWidgets={false}
        onSuggestionClick={(value) => { if (!running) setDraft(value); }} />
    </div>
    {ticket && descriptor.progress && <section aria-label="未保存の応答" data-thread-progress
      className="min-w-0 rounded border border-zinc-700 p-3">
      <p role="status" className="text-xs text-zinc-400">{visibleProgress?.finishSeen
        ? "プロバイダーの応答を受信しました。保存結果の照合を待っています。"
        : progressStatus === "rejected" ? "応答の識別情報や順序を照合できません。入力内容と送信IDを保持しています。"
          : progressStatus === "unavailable" ? "ライブ応答は現在利用できません。保存された実行記録の照合を続けます。"
            : "未保存の応答を照合しています。確定した履歴は上に表示します。"}</p>
      {visibleProgress?.text && <pre aria-label="受信した未保存テキスト" data-thread-progress-text
        className="whitespace-pre-wrap break-words text-sm">{visibleProgress.text}</pre>}
    </section>}
    {ticket && <button type="button" onClick={() => { void readEvents(); }} className="min-h-11 rounded border border-zinc-700 px-3 text-sm">実行記録を再読み込み</button>}
    {ticket && descriptor.reconcile && <button type="button"
      disabled={!recoveryRequest || busy || recovering} onClick={() => { void reconcile(); }}
      className="min-h-11 rounded border border-zinc-700 px-3 text-sm">
      {recovering ? "復旧結果を待っています…" : "この送信結果を復旧"}
    </button>}
    <ComposerRenderer surfaceMode="thread" input={draft} placeholder="メッセージを入力…"
      isGenerating={running} submissionDisabled={!sourceReady || !parsed}
      selectedProfile={null} favoriteProfiles={[]} thinkingLevel={null}
      contextUsage={{ ratio: 0, usedTokens: 0, maxContext: 0, label: "" }}
      inlineExtensions={[]} belowExtensions={[]} voiceInputEnabled={false}
      manualRuntimeModeSelectionEnabled={false} mode="chat"
      widgetContext={createWidgetConversationContext(thread.conversation.id)}
      voiceScopeKey={`${registered.reference.profileId}:${thread.conversation.id}`}
      onInputChange={setDraft} onSubmit={(event) => { event.preventDefault(); void send(); }}
      onStopGenerating={stopRequest ? () => { void stop(); } : undefined}
      onModelProfileSelect={() => undefined} onThinkingLevelChange={() => undefined} />
  </div>;
}
