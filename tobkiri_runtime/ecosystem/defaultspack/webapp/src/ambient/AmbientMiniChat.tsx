import type { FormEvent } from "react";
import { ExternalLink, Loader2, MessageSquare, RefreshCcw, SendHorizontal, ShieldAlert } from "lucide-react";

import type { Conversation } from "../lib/api";
import type { AuthorityApproval } from "../lib/authorityApproval";
import { cn } from "../lib/cn";
import { ErrorNotice } from "../components/ErrorNotice";
import { ambientMiniChatMessages } from "./ambientMiniChatState";

type Props = {
  conversation: Conversation | null;
  conversationId: string | null;
  routingSummary: string;
  loading: boolean;
  error: string | null;
  input: string;
  sending: boolean;
  disabled: boolean;
  latestInputPreview: string | null;
  authorityApproval?: AuthorityApproval | null;
  authorityApprovalUrl?: string | null;
  showPicker?: boolean;
  onInputChange: (value: string) => void;
  onSubmit: (event: FormEvent<HTMLFormElement>) => void;
  onOpenChat?: () => void;
  onRefresh: () => void;
  onPickChat: () => void;
  onOpenAuthorityApproval?: () => void;
};

export function AmbientMiniChat({
  conversation,
  conversationId,
  routingSummary,
  loading,
  error,
  input,
  sending,
  disabled,
  latestInputPreview,
  authorityApproval = null,
  authorityApprovalUrl = null,
  showPicker = true,
  onInputChange,
  onSubmit,
  onOpenChat,
  onRefresh,
  onPickChat,
  onOpenAuthorityApproval,
}: Props) {
  const messages = ambientMiniChatMessages(conversation, 5);
  const latestPreview = String(latestInputPreview ?? "").trim();
  const showLatestPreview = Boolean(latestPreview) && !messages.some((message) => (
    message.role === "user" && normalizeText(message.text) === normalizeText(latestPreview)
  ));
  const title = conversation?.title?.trim() || (conversationId ? "Linked chat" : "チャット");

  return (
    <section data-testid="ambient-mini-chat" className="flex min-h-[178px] flex-1 flex-col rounded-lg border border-zinc-800 bg-zinc-950/70">
      <div className="flex items-center gap-2 border-b border-zinc-800/75 px-2.5 py-2">
        <MessageSquare size={13} className="shrink-0 text-sky-200" />
        <div className="min-w-0 flex-1">
          <p className="truncate text-[12px] font-semibold text-zinc-100">{title}</p>
          <p className="truncate text-[10px] text-zinc-500">{conversationId || routingSummary}</p>
        </div>
        {showPicker && (
          <button
            type="button"
            onClick={onPickChat}
            className="ambient-mini-button h-7 shrink-0 px-2"
            title="チャットを選ぶ"
            aria-label="チャットを選ぶ"
          >
            {conversationId ? "切替" : "選ぶ"}
          </button>
        )}
        {conversationId && onOpenChat && (
          <button
            type="button"
            data-testid="ambient-mini-chat-open"
            onClick={onOpenChat}
            className="ambient-mini-button h-7 shrink-0 px-2"
            title={`/chat?chat=${conversationId}`}
            aria-label="チャットを開く"
          >
            <ExternalLink size={12} />
            開く
          </button>
        )}
        <button
          type="button"
          onClick={onRefresh}
          disabled={loading || !conversationId}
          className="inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-md border border-zinc-800 text-zinc-400 hover:border-zinc-700 hover:text-zinc-100 disabled:cursor-not-allowed disabled:opacity-35"
          title="更新"
          aria-label="チャットを更新"
        >
          {loading ? <Loader2 size={12} className="animate-spin" /> : <RefreshCcw size={12} />}
        </button>
      </div>

      <div data-testid="ambient-mini-chat-output" className="min-h-0 flex-1 space-y-1.5 overflow-y-auto px-2.5 py-2">
        {messages.length === 0 && !showLatestPreview && (
          <div className="rounded-md border border-zinc-800 bg-black/20 px-2 py-2 text-[11px] leading-5 text-zinc-500">
            {conversationId ? "まだ表示できるメッセージはありません。" : "送信先チャットは次の入力で確定します。"}
          </div>
        )}
        {messages.map((message) => (
          <MiniChatBubble key={message.id} role={message.role} text={message.text} />
        ))}
        {showLatestPreview && <MiniChatBubble role="user" text={latestPreview} pending />}
        {authorityApproval && (
          <div className="rounded-md border border-sky-300/25 bg-sky-400/10 px-2 py-2 text-[11px] leading-5 text-sky-50">
            <div className="flex items-start gap-2">
              <ShieldAlert size={14} className="mt-0.5 shrink-0 text-sky-200" />
              <div className="min-w-0 flex-1">
                <p className="font-semibold">AIの使用を許可</p>
                <p className="text-sky-100/75">承認後に回答を続行します。</p>
              </div>
            </div>
            <div className="mt-2 flex flex-wrap gap-1.5">
                <button
                  type="button"
                  onClick={onOpenAuthorityApproval}
                  className="inline-flex h-7 items-center gap-1.5 rounded-md border border-sky-200/35 bg-sky-100 px-2 text-[11px] font-semibold text-zinc-950 hover:bg-white"
                  title="承認を開く"
                >
                  <ExternalLink size={12} />
                  承認を開く
                </button>
                {authorityApprovalUrl && (
                  <a
                    href={authorityApprovalUrl}
                    rel="noreferrer"
                    className="inline-flex h-7 items-center gap-1.5 rounded-md border border-sky-200/35 px-2 text-[11px] font-semibold text-sky-50 hover:bg-sky-300/10"
                    title="同じタブで承認を開く"
                  >
                    <ExternalLink size={12} />
                    同じタブ
                  </a>
                )}
            </div>
          </div>
        )}
        {error && (
          <ErrorNotice
            className="px-2 py-1.5 text-[11px] leading-5"
            copyLabel="ミニチャットエラーをコピー"
            message={error}
          />
        )}
      </div>

      <form onSubmit={onSubmit} className="flex gap-1.5 border-t border-zinc-800/75 p-2">
        <input
          data-testid="ambient-mini-chat-input"
          value={input}
          onChange={(event) => onInputChange(event.target.value)}
          disabled={disabled || sending}
          placeholder={disabled ? "送信許可が必要です" : "メッセージ"}
          className="h-8 min-w-0 flex-1 rounded-md border border-zinc-800 bg-zinc-950 px-2 text-xs text-zinc-100 outline-none placeholder:text-zinc-600 focus:border-sky-300/45 disabled:cursor-not-allowed disabled:opacity-45"
        />
        <button
          type="submit"
          disabled={disabled || sending || !input.trim()}
          className="inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-md border border-sky-300/35 bg-sky-400/10 text-sky-100 hover:border-sky-200/50 disabled:cursor-not-allowed disabled:opacity-35"
          title="送信"
          aria-label="送信"
        >
          {sending ? <Loader2 size={14} className="animate-spin" /> : <SendHorizontal size={14} />}
        </button>
      </form>
    </section>
  );
}

function MiniChatBubble({ role, text, pending = false }: { role: "user" | "assistant"; text: string; pending?: boolean }) {
  const isUser = role === "user";
  return (
    <div className={cn("flex", isUser ? "justify-end" : "justify-start")}>
      <div
        className={cn(
          "max-w-[88%] whitespace-pre-wrap rounded-lg px-2.5 py-1.5 text-[11px] leading-5",
          isUser
            ? "border border-sky-300/25 bg-sky-400/15 text-sky-50"
            : "border border-zinc-800 bg-zinc-900/75 text-zinc-200",
          pending && "border-dashed opacity-80",
        )}
      >
        {text}
      </div>
    </div>
  );
}

function normalizeText(value: string): string {
  return value.replace(/\s+/g, " ").trim();
}
