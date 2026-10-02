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
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [feedback, setFeedback] = useState<string | null>(null);
  const active = useRef(true);
  const pending = useRef(false);
  const reading = useRef(false);
  const stopping = useRef(false);
  const state = useRef({ draft, ticket, activeTurnId: thread?.turn?.id });
  state.current = { draft, ticket, activeTurnId: ticket?.turnId ?? thread?.turn?.id };
  const endRef = useRef<HTMLDivElement | null>(null);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const ownerId = JSON.stringify([registered.reference, "conversation_thread", viewContextKey(registered, context)]);
  const canLeave = useCallback(() => {
    const current = state.current;
    if (pending.current || current.ticket) return false;
    return !current.draft || window.confirm("Discard this unsent message?");
  }, []);
  useEffect(() => {
    active.current = true;
    onDirtyChange?.(ownerId, canLeave);
    return () => { active.current = false; onDirtyChange?.(ownerId, null); };
  }, [onDirtyChange, ownerId, canLeave]);
  useEffect(() => {
    const guard = (event: BeforeUnloadEvent) => {
      if (!pending.current && !state.current.ticket && !state.current.draft) return;
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

  if (!descriptor || !thread) return <p role="status">この会話はまだ利用できません。利用可能な操作で会話を作成するか、再読み込みしてください。</p>;
  const messages = orderConversationMessages(thread.messages).map((message) => chatMessageToUiMessage(message));
  const start = thread.turn?.started_at_ms;
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
    {ticket && <button type="button" onClick={() => { void readEvents(); }} className="min-h-11 rounded border border-zinc-700 px-3 text-sm">実行結果を照合</button>}
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
