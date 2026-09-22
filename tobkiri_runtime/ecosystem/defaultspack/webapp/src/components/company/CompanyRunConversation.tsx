import { MessageSquareText } from "lucide-react";

import type { CompanyRunConversationMessage } from "../../lib/api";

const ROLE_TONES: Record<string, { rail: string; label: string; text: string }> = {
  user: { rail: "border-sky-500/60", label: "text-sky-300", text: "text-zinc-200" },
  assistant: { rail: "border-emerald-500/60", label: "text-emerald-300", text: "text-zinc-200" },
  error: { rail: "border-rose-500/70", label: "text-rose-300", text: "text-rose-100" },
  tool: { rail: "border-amber-500/50", label: "text-amber-300", text: "text-zinc-300" },
  function: { rail: "border-amber-500/50", label: "text-amber-300", text: "text-zinc-300" },
};

function toneFor(message: CompanyRunConversationMessage) {
  if (message.is_error) return ROLE_TONES.error;
  return ROLE_TONES[message.role] ?? { rail: "border-zinc-700", label: "text-zinc-400", text: "text-zinc-300" };
}

export function CompanyRunConversation({
  messages,
  fallback,
  fallbackError = false,
}: {
  messages?: CompanyRunConversationMessage[];
  fallback?: string | null;
  fallbackError?: boolean;
}) {
  const visibleMessages =
    messages && messages.length > 0
      ? messages
      : fallback
        ? [
            {
              role: fallbackError ? "error" : "assistant",
              label: fallbackError ? "Agent error" : "Agent reply",
              content: fallback,
              is_error: fallbackError,
            },
          ]
        : [];

  if (visibleMessages.length === 0) return null;

  return (
    <div className="mt-2 border-t border-zinc-800/70 pt-2" aria-label="Subagent conversation">
      <div className="mb-1.5 flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-wide text-zinc-500">
        <MessageSquareText size={11} />
        <span>Subagent Conversation</span>
      </div>
      <div className="space-y-2">
        {visibleMessages.map((message, index) => {
          const tone = toneFor(message);
          return (
            <div key={`${message.role}:${index}`} className={`border-l-2 pl-2 ${tone.rail}`}>
              <div className="mb-0.5 flex min-w-0 items-center justify-between gap-2 text-[9px] uppercase tracking-wide">
                <span className={`truncate font-semibold ${tone.label}`}>{message.label || message.role}</span>
                <span className="flex-shrink-0 font-mono text-zinc-700">{message.role}</span>
              </div>
              <p className={`max-h-32 overflow-auto whitespace-pre-wrap break-words text-[10px] leading-relaxed ${tone.text}`}>
                {message.content}
              </p>
            </div>
          );
        })}
      </div>
    </div>
  );
}
