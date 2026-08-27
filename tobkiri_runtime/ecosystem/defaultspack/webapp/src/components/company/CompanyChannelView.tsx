import { MessageSquare, Send } from "lucide-react";
import { useMemo, useState } from "react";

import {
  createCompanyOperationId,
  pendingCompanyAction,
  rejectedCompanyAction,
  type CompanyActionState,
  type CompanyMutationReceipt,
} from "../../features/company/companyWorkspaceState";
import type { CompanyChannel, CompanyMessage } from "../../lib/api";

function positiveCount(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) && value > 0 ? value : 0;
}

function messageTime(value: string | undefined): number | null {
  const time = Date.parse(value ?? "");
  return Number.isFinite(time) ? time : null;
}

function textValue(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

export function companyMessageChannelId(message: CompanyMessage): string {
  return (
    textValue(message.channel_id)
    || textValue(message.metadata?.channel_id)
    || textValue(message.metadata?.channelId)
    || textValue(message.metadata?.channel)
  );
}

export function visibleCompanyMessagesForChannel(
  messages: CompanyMessage[],
  selectedChannelId?: string | null,
  limit = 20,
): CompanyMessage[] {
  return messages
    .filter((message) => !selectedChannelId || companyMessageChannelId(message) === selectedChannelId)
    .map((message, index) => ({ message, index }))
    .sort((left, right) => {
      const leftTime = messageTime(left.message.created_at);
      const rightTime = messageTime(right.message.created_at);
      if (leftTime !== null && rightTime !== null && leftTime !== rightTime) {
        return leftTime - rightTime;
      }
      return left.index - right.index;
    })
    .slice(-limit)
    .map((item) => item.message);
}

export function CompanyChannelView({
  channels,
  messages,
  activeChannelId,
  busy = false,
  onChannelChange,
  onSendMessage,
}: {
  channels: CompanyChannel[];
  messages: CompanyMessage[];
  activeChannelId?: string | null;
  busy?: boolean;
  onChannelChange?: (channelId: string) => void;
  onSendMessage?: (
    content: string,
    channelId: string,
    operationId: string,
  ) => Promise<CompanyMutationReceipt<CompanyMessage>>;
}) {
  const [draft, setDraft] = useState("");
  const [sendState, setSendState] = useState<CompanyActionState>({ phase: "idle" });
  const [lastAttempt, setLastAttempt] = useState<{
    content: string;
    channelId: string;
    operationId: string;
  } | null>(null);
  const [pendingChannelId, setPendingChannelId] = useState<string | null>(null);
  const selectedChannelId = activeChannelId || channels[0]?.id || "ops-company";
  const activeChannel = useMemo(
    () => channels.find((channel) => channel.id === selectedChannelId) ?? channels[0] ?? null,
    [channels, selectedChannelId],
  );
  const expectedMessageCount = positiveCount(activeChannel?.message_count);
  const visibleMessages = useMemo(() => {
    const scopedMessages = visibleCompanyMessagesForChannel(messages, selectedChannelId);
    if (scopedMessages.length > 0 || expectedMessageCount === 0 || messages.length === 0) {
      return scopedMessages;
    }
    const knownChannelIds = new Set(channels.map((channel) => channel.id));
    const messagesHaveKnownChannels = messages.some((message) => knownChannelIds.has(companyMessageChannelId(message)));
    if (channels.length <= 1 || !messagesHaveKnownChannels) {
      return visibleCompanyMessagesForChannel(messages, null);
    }
    return scopedMessages;
  }, [channels, expectedMessageCount, messages, selectedChannelId]);
  const sendPending = sendState.phase === "pending";

  const submitMessage = async (attempt?: typeof lastAttempt): Promise<boolean> => {
    if (!onSendMessage || sendPending) return false;
    const content = attempt?.content ?? draft.trim();
    const channelId = attempt?.channelId ?? selectedChannelId;
    if (!content) return false;
    const operationId = attempt?.operationId ?? createCompanyOperationId("company-message");
    const nextAttempt = { content, channelId, operationId };
    setLastAttempt(nextAttempt);
    setSendState(pendingCompanyAction(operationId));
    try {
      const receipt = await onSendMessage(content, channelId, operationId);
      if (receipt.phase === "committed") {
        setSendState({
          phase: "committed",
          operationId,
          message: "Message sent",
          updatedAt: Date.now(),
        });
        setDraft((current) => current.trim() === content ? "" : current);
        setLastAttempt(null);
        return true;
      }
      setSendState({
        phase: "rejected",
        operationId,
        message: receipt.error ?? "The message was not sent. Your draft was kept.",
        retryable: receipt.retryable ?? true,
        ambiguous: receipt.ambiguous,
      });
      return false;
    } catch (error) {
      setSendState(rejectedCompanyAction(operationId, error));
      return false;
    }
  };

  return (
    <section className="space-y-2 p-2">
      <div className="flex items-center justify-between gap-2">
        <h4 className="text-[11px] font-semibold uppercase tracking-wide text-zinc-500">Channels</h4>
        <select
          value={selectedChannelId}
          onChange={(event) => {
            if (draft.trim()) {
              setPendingChannelId(event.target.value);
              return;
            }
            setSendState({ phase: "idle" });
            onChannelChange?.(event.target.value);
          }}
          disabled={sendPending}
          className="max-w-[150px] bg-transparent text-[11px] text-zinc-300 outline-none"
        >
          {channels.map((channel) => (
            <option key={channel.id} value={channel.id} className="bg-zinc-900">
              {channel.name || channel.id}
            </option>
          ))}
          {channels.length === 0 && <option value="ops-company">ops-company</option>}
        </select>
      </div>

      <div className="max-h-48 space-y-1 overflow-y-auto">
        {visibleMessages.map((message) => (
          <div key={message.id} className="rounded-md border border-zinc-800/70 bg-zinc-950/40 px-2 py-1.5">
            <div className="mb-0.5 flex items-center gap-1.5 text-[10px] text-zinc-500">
              <MessageSquare size={10} />
              <span className="truncate">{message.sender_id}</span>
            </div>
            <p className="line-clamp-3 whitespace-pre-wrap text-[11px] leading-relaxed text-zinc-300">{message.content}</p>
          </div>
        ))}
        {visibleMessages.length === 0 && (
          <div className="rounded-md border border-zinc-800/70 bg-zinc-950/40 px-2 py-2 text-[11px] text-zinc-500">
            {expectedMessageCount > 0
              ? `${expectedMessageCount} messages recorded. Refreshing messages...`
              : "No messages in this channel."}
          </div>
        )}
      </div>

      {onSendMessage && (
        <form
          className="flex items-center gap-1.5"
          onSubmit={(event) => {
            event.preventDefault();
            void submitMessage();
          }}
        >
          <input
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            disabled={busy || sendPending}
            placeholder="@pm handoff note"
            className="h-8 min-w-0 flex-1 rounded-md border border-zinc-800 bg-zinc-950 px-2 text-[12px] text-zinc-200 outline-none placeholder:text-zinc-600 focus:border-zinc-600"
          />
          <button
            type="submit"
            disabled={busy || sendPending || !draft.trim()}
            aria-busy={sendPending}
            className="flex h-8 w-8 items-center justify-center rounded-md bg-zinc-100 text-zinc-950 hover:bg-white disabled:opacity-30"
            title="Send company message"
          >
            <Send size={13} />
          </button>
        </form>
      )}
      {sendState.phase !== "idle" && (
        <div
          role={sendState.phase === "rejected" ? "alert" : "status"}
          aria-live={sendState.phase === "rejected" ? "assertive" : "polite"}
          className={`flex items-center justify-between gap-2 rounded-md border px-2 py-1.5 text-[11px] ${
            sendState.phase === "rejected"
              ? "border-amber-500/30 bg-amber-500/10 text-amber-200"
              : "border-emerald-500/20 bg-emerald-500/10 text-emerald-200"
          }`}
        >
          <span>{sendState.message}</span>
          {sendState.phase === "rejected" && sendState.retryable && lastAttempt && (
            <button
              type="button"
              onClick={() => void submitMessage(lastAttempt)}
              className="rounded border border-amber-400/30 px-2 py-1 font-medium hover:bg-amber-400/10"
            >
              Retry
            </button>
          )}
        </div>
      )}
      {pendingChannelId && (
        <div role="alertdialog" aria-label="Unsent Company channel message" className="space-y-2 rounded-md border border-sky-500/30 bg-sky-500/10 p-2 text-[11px] text-sky-100">
          <p>Send or discard your draft before switching channels.</p>
          <div className="flex flex-wrap gap-1.5">
            <button
              type="button"
              disabled={sendPending}
              onClick={() => void submitMessage().then((sent) => {
                if (!sent) return;
                const channelId = pendingChannelId;
                setPendingChannelId(null);
                onChannelChange?.(channelId);
              })}
              className="rounded border border-sky-300/30 px-2 py-1"
            >
              Send &amp; switch
            </button>
            <button
              type="button"
              disabled={sendPending}
              onClick={() => {
                const channelId = pendingChannelId;
                setDraft("");
                setSendState({ phase: "idle" });
                setPendingChannelId(null);
                onChannelChange?.(channelId);
              }}
              className="rounded border border-zinc-600 px-2 py-1"
            >
              Discard &amp; switch
            </button>
            <button type="button" disabled={sendPending} onClick={() => setPendingChannelId(null)} className="rounded border border-zinc-600 px-2 py-1">Cancel</button>
          </div>
        </div>
      )}
    </section>
  );
}
