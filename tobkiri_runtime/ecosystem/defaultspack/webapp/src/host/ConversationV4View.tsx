import { useRef, useState, type FormEvent } from "react";

import { ErrorNotice } from "../components/ErrorNotice";
import type {
  FrontendCapabilityClient,
  VerifiedFrontendContribution,
} from "./frontendContracts";

export type ConversationV4Message = {
  id: string;
  role: "user" | "assistant";
  content: string;
};

type ContractMessage = Pick<ConversationV4Message, "role" | "content">;

const CONVERSATION_V4_CONTRACT = "conversation.turn.v1";
const CONVERSATION_V4_CONTRIBUTION = "defaults.conversation.complete";
const CONVERSATION_V4_BUILD_IDENTITY = "defaultspack.conversation";

/** Return whether a verified contribution selects the host-owned v4 chat. */
export function isConversationV4Contribution(
  item: VerifiedFrontendContribution,
): boolean {
  return item.kind === "route"
    && item.mode === "declarative"
    && item.route === "/chat"
    && item.contribution_id === CONVERSATION_V4_CONTRIBUTION
    && item.owner_pack_id === "defaultspack"
    && item.build_identity === CONVERSATION_V4_BUILD_IDENTITY
    && item.action_contract === CONVERSATION_V4_CONTRACT
    && item.view?.type === "conversation_v4";
}

/** Build the sole capability payload accepted by the Pack v4 conversation. */
export function conversationV4CapabilityPayload(
  messages: readonly ConversationV4Message[],
): Record<string, unknown> {
  return {
    messages: messages.map(({ role, content }): ContractMessage => ({
      role,
      content,
    })),
  };
}

/** Convert a bounded host capability failure into an accessible UI message. */
export function frontendActionErrorMessage(error: unknown): string {
  const code = asRecord(error)?.code;
  if (code === "STALE_RESOLUTION" || code === "STALE_CATALOG") {
    return "This screen is out of date and is refreshing. Try the action again.";
  }
  if (error instanceof Error && error.message.trim()) return error.message;
  return "The action could not be completed.";
}

/** Extract the text projection from the non-streaming conversation contract. */
export function conversationV4AssistantText(result: unknown): string | null {
  const resultRecord = asRecord(result);
  if (!resultRecord) return typeof result === "string" ? result : null;

  for (const candidate of [resultRecord, asRecord(resultRecord.data)]) {
    if (!candidate) continue;
    const content = contentText(candidate.content);
    if (content) return content;
    const output = contentText(candidate.output);
    if (output) return output;
    if (typeof candidate.message === "string" && candidate.message.trim()) {
      return candidate.message;
    }
  }
  return null;
}

/** Render the minimal, host-owned complete-only Pack v4 conversation surface. */
export function ConversationV4View({
  item,
  catalogHash,
  capabilities,
}: {
  item: VerifiedFrontendContribution;
  catalogHash: string;
  capabilities: FrontendCapabilityClient;
}) {
  const [messages, setMessages] = useState<ConversationV4Message[]>([]);
  const [draft, setDraft] = useState("");
  const [isSending, setIsSending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [retryMessages, setRetryMessages] = useState<ConversationV4Message[] | null>(
    null,
  );
  const sendingRef = useRef(false);

  const complete = async (transcript: ConversationV4Message[]): Promise<void> => {
    if (sendingRef.current || transcript.length === 0) return;
    sendingRef.current = true;
    setIsSending(true);
    setError(null);
    setRetryMessages(null);
    try {
      const result = await capabilities.invokeAction({
        contractId: CONVERSATION_V4_CONTRACT,
        payload: conversationV4CapabilityPayload(transcript),
        contributionId: item.contribution_id,
        ownerPackId: item.owner_pack_id,
        planHash: item.resolved_plan_hash,
        catalogHash,
      });
      const content = conversationV4AssistantText(result);
      if (!content) {
        throw new Error("The conversation completed without an assistant message.");
      }
      setMessages([
        ...transcript,
        {
          id: conversationV4MessageId("assistant"),
          role: "assistant",
          content,
        },
      ]);
    } catch (completionError) {
      setError(frontendActionErrorMessage(completionError));
      setRetryMessages(transcript);
    } finally {
      sendingRef.current = false;
      setIsSending(false);
    }
  };

  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!draft.trim() || isSending) return;
    const transcript = [
      ...messages,
      {
        id: conversationV4MessageId("user"),
        role: "user" as const,
        content: draft,
      },
    ];
    setMessages(transcript);
    setDraft("");
    void complete(transcript);
  };

  return (
    <main
      aria-labelledby="conversation-v4-title"
      className="rumi-app-shell flex min-h-0 w-full flex-col bg-[#09090b] text-zinc-100"
      data-contribution-id={item.contribution_id}
      data-conversation-surface="v4"
    >
      <header className="border-b border-zinc-800/80 px-5 py-4 sm:px-7">
        <p className="text-xs font-semibold uppercase tracking-[0.2em] text-sky-200/80">
          Pack v4
        </p>
        <h1
          className="mt-1 text-xl font-semibold tracking-tight text-zinc-50"
          id="conversation-v4-title"
        >
          Tobkiri Conversation
        </h1>
      </header>

      <section
        aria-label="Conversation transcript"
        className="min-h-0 flex-1 overflow-y-auto px-5 py-6 sm:px-7"
      >
        {messages.length === 0 ? (
          <p className="mx-auto max-w-2xl pt-[12vh] text-center text-sm leading-6 text-zinc-400">
            Start a conversation with Tobkiri.
          </p>
        ) : (
          <div
            aria-atomic="false"
            aria-live="polite"
            aria-relevant="additions text"
            className="mx-auto flex max-w-3xl flex-col gap-4"
            role="log"
          >
            {messages.map((message) => (
              <article
                aria-label={message.role === "user" ? "You" : "Tobkiri"}
                className={message.role === "user"
                  ? "self-end rounded-2xl rounded-br-md bg-sky-400 px-4 py-3 text-sm leading-6 text-slate-950"
                  : "self-start rounded-2xl rounded-bl-md border border-zinc-800 bg-zinc-900/70 px-4 py-3 text-sm leading-6 text-zinc-100"
                }
                data-conversation-role={message.role}
                key={message.id}
              >
                <p className="mb-1 text-xs font-semibold opacity-75">
                  {message.role === "user" ? "You" : "Tobkiri"}
                </p>
                <p className="whitespace-pre-wrap break-words">{message.content}</p>
              </article>
            ))}
          </div>
        )}
      </section>

      <div className="border-t border-zinc-800/80 bg-zinc-950/90 px-5 py-4 sm:px-7">
        <form className="mx-auto max-w-3xl" onSubmit={submit}>
          <label className="block text-sm font-medium text-zinc-200" htmlFor="conversation-v4-input">
            Message Tobkiri
          </label>
          <textarea
            aria-describedby="conversation-v4-help"
            className="mt-2 min-h-24 w-full resize-y rounded-xl border border-zinc-700 bg-zinc-900 px-3 py-2.5 text-sm leading-6 text-zinc-100 outline-none placeholder:text-zinc-500 focus-visible:border-sky-300 focus-visible:ring-2 focus-visible:ring-sky-400/30 disabled:cursor-wait disabled:opacity-70"
            disabled={isSending}
            id="conversation-v4-input"
            onChange={(event) => setDraft(event.target.value)}
            placeholder="Ask anything…"
            value={draft}
          />
          <p className="mt-2 text-xs text-zinc-500" id="conversation-v4-help">
            Sends this complete in-memory conversation to the active Pack v4 profile.
          </p>
          {isSending ? (
            <p className="mt-3 text-sm text-sky-100" role="status">
              Tobkiri is thinking…
            </p>
          ) : null}
          {error ? (
            <ErrorNotice
              className="mt-3 text-sm"
              copyLabel="Copy conversation error"
              copyText={error}
              errorIcon="conversation-v4"
              message={error}
              trailing={retryMessages ? (
                <button
                  className="rounded-lg border border-red-300/40 px-3 py-1.5 text-sm font-semibold text-red-100 hover:bg-red-400/10 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-red-300"
                  onClick={() => void complete(retryMessages)}
                  type="button"
                >
                  Try again
                </button>
              ) : undefined}
            />
          ) : null}
          <div className="mt-4 flex justify-end">
            <button
              className="rounded-xl bg-sky-300 px-4 py-2 text-sm font-semibold text-slate-950 transition hover:bg-sky-200 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sky-200 focus-visible:ring-offset-2 focus-visible:ring-offset-zinc-950 disabled:cursor-not-allowed disabled:opacity-50"
              disabled={isSending || !draft.trim()}
              type="submit"
            >
              {isSending ? "Sending…" : "Send"}
            </button>
          </div>
        </form>
      </div>
    </main>
  );
}

/** Show a scoped failure state instead of starting the legacy chat surface. */
export function ConversationV4Unavailable({
  reason,
  onRetry,
}: {
  reason: string;
  onRetry: () => void;
}) {
  return (
    <main
      aria-labelledby="conversation-v4-unavailable-title"
      className="flex min-h-screen items-center justify-center bg-[#09090b] px-6 py-10 text-zinc-100"
      data-conversation-surface="v4-unavailable"
    >
      <section className="w-full max-w-lg rounded-2xl border border-zinc-800 bg-zinc-950 p-6 shadow-2xl">
        <p className="text-xs font-semibold uppercase tracking-[0.2em] text-amber-200/80">
          Pack v4
        </p>
        <h1 className="mt-2 text-xl font-semibold" id="conversation-v4-unavailable-title">
          Tobkiri Conversation is unavailable
        </h1>
        <ErrorNotice
          className="mt-3 text-sm leading-6"
          copyLabel="Copy unavailable conversation error"
          copyText={reason}
          errorIcon="conversation-v4-unavailable"
          message={reason}
          trailing={(
            <button
              className="rounded-xl border border-zinc-600 px-4 py-2 text-sm font-semibold text-zinc-100 hover:bg-zinc-900 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-zinc-300"
              onClick={onRetry}
              type="button"
            >
              Retry
            </button>
          )}
        />
      </section>
    </main>
  );
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

function contentText(value: unknown): string | null {
  if (typeof value === "string" && value.trim()) return value;
  if (!Array.isArray(value)) return null;
  const text = value.flatMap((item) => {
    if (typeof item === "string") return [item];
    const block = asRecord(item);
    if (!block) return [];
    if (typeof block.text === "string") return [block.text];
    if (typeof block.content === "string") return [block.content];
    const delta = asRecord(block.delta);
    return typeof delta?.text === "string" ? [delta.text] : [];
  }).join("");
  return text.trim() ? text : null;
}

function conversationV4MessageId(role: ConversationV4Message["role"]): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return `${role}-${crypto.randomUUID()}`;
  }
  return `${role}-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}
