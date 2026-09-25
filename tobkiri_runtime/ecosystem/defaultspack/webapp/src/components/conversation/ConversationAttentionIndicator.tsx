import { CircleAlert, CircleX, LoaderCircle } from "lucide-react";

import type { ConversationPresentation } from "../../features/conversations/conversationPresentation";
import { cn } from "../../lib/cn";

export function ConversationAttentionIndicator({
  presentation,
  className,
}: {
  presentation: ConversationPresentation;
  className?: string;
}) {
  if (
    (presentation.activity === "idle" || presentation.activity === "done")
    && !presentation.unread
  ) return null;

  const commonProps = {
    size: 11,
    strokeWidth: 2.2,
    "aria-hidden": true,
  } as const;
  const marker = presentation.activity === "running"
    ? <LoaderCircle {...commonProps} className="animate-spin motion-reduce:animate-none" />
    : presentation.activity === "waiting"
      ? <CircleAlert {...commonProps} />
      : presentation.activity === "failed"
        ? <CircleX {...commonProps} />
        : <span className="h-1.5 w-1.5 rounded-full bg-current" aria-hidden="true" />;

  return (
    <span
      role="status"
      aria-label={presentation.accessibleStatusLabel ?? "Conversation update"}
      title={presentation.accessibleStatusLabel ?? undefined}
      data-conversation-activity={presentation.activity}
      data-conversation-unread={presentation.unread ? "true" : "false"}
      className={cn(
        "flex h-4 w-4 shrink-0 items-center justify-center",
        presentation.activity === "failed"
          ? "text-red-400"
          : presentation.activity === "waiting"
            ? "text-amber-300"
            : presentation.activity === "running"
              ? "text-sky-300"
              : "text-emerald-300",
        className,
      )}
    >
      {marker}
    </span>
  );
}
