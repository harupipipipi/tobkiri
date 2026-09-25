import { MessageSquare, Send } from "lucide-react";
import { useMemo, useState } from "react";

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
  onSendMessage?: (content: string, channelId: string) => void | Promise<void>;
}) {
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  const [sendStatus, setSendStatus] = useState("");
  const [sendError, setSendError] = useState("");
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
  const controlsDisabled = busy || sending;
  const activeChannelName = activeChannel?.name || activeChannel?.id || selectedChannelId;

  return (
    <section aria-labelledby="company-channels-title" aria-busy={controlsDisabled} className="space-y-2 p-2">
      <div className="flex items-center justify-between gap-2">
        <h4 id="company-channels-title" className="text-[11px] font-semibold uppercase tracking-wide text-zinc-500">Channels</h4>
        <label htmlFor="company-channel-select" className="sr-only">Active company channel</label>
        <select
          id="company-channel-select"
          value={selectedChannelId}
          onChange={(event) => onChannelChange?.(event.target.value)}
          disabled={controlsDisabled}
          aria-describedby="company-channel-help"
          className="min-h-11 max-w-[180px] bg-transparent text-[11px] text-zinc-300 outline-none"
        >
          {channels.map((channel) => (
            <option key={channel.id} value={channel.id} className="bg-zinc-900">
              {channel.name || channel.id}
            </option>
          ))}
          {channels.length === 0 && <option value="ops-company">ops-company</option>}
        </select>
      </div>
      <p id="company-channel-help" className="sr-only">Choose which Company channel to read and send messages to.</p>

      <div role="log" aria-live="polite" aria-relevant="additions" aria-label={`Messages in ${activeChannelName}`} className="max-h-48 space-y-1 overflow-y-auto">
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
          aria-label={`Send a message to ${activeChannelName}`}
          className="flex items-center gap-1.5"
          onSubmit={async (event) => {
            event.preventDefault();
            const content = draft.trim();
            if (!content) return;
            setSending(true);
            setSendError("");
            setSendStatus(`Sending message to ${activeChannelName}`);
            try {
              await onSendMessage(content, selectedChannelId);
              setDraft("");
              setSendStatus(`Message sent to ${activeChannelName}`);
            } catch (error) {
              setSendStatus("");
              setSendError(error instanceof Error ? error.message : `Message failed for ${activeChannelName}`);
            } finally {
              setSending(false);
            }
          }}
        >
          <label htmlFor="company-channel-message" className="sr-only">Message for {activeChannelName}</label>
          <input
            id="company-channel-message"
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            disabled={controlsDisabled}
            required
            aria-required="true"
            aria-describedby="company-channel-message-help company-channel-send-status"
            placeholder="@pm handoff note"
            className="h-11 min-w-0 flex-1 rounded-md border border-zinc-800 bg-zinc-950 px-2 text-[12px] text-zinc-200 outline-none placeholder:text-zinc-600 focus:border-zinc-600"
          />
          <button
            type="submit"
            disabled={controlsDisabled || !draft.trim()}
            aria-label={`Send message to ${activeChannelName}`}
            className="flex h-11 w-11 items-center justify-center rounded-md bg-zinc-100 text-zinc-950 hover:bg-white focus-visible:outline focus-visible:outline-2 focus-visible:outline-emerald-300 disabled:opacity-30"
            title="Send company message"
          >
            <Send size={13} />
          </button>
        </form>
      )}
      <p id="company-channel-message-help" className="sr-only">The draft is cleared only after the Company message request succeeds.</p>
      <div id="company-channel-send-status" role="status" aria-live="polite" aria-atomic="true" className="sr-only">{sendStatus}</div>
      {sendError ? <p role="alert" className="text-[11px] text-red-300">{sendError}</p> : null}
    </section>
  );
}
