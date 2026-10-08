import type { ChatContentBlock, ChatMessage, ModelProfile } from "./api";
import type { ChatUiMessage, DroppedWidget } from "../renderers/types";
import { savedChatReferenceMentions } from "./savedChatReferenceMentions";
import { boundedDurationLabel } from "./duration";
import { formatRelativeTime, messageToText } from "./chat";
import { composerMentionMetadataFromWidgets, normalizeComposerMentionMetadata } from "./composerWidgets";

function normalizeBlocks(message: ChatMessage): ChatContentBlock[] {
  if (typeof message.content === "string") {
    return [{ type: "text", text: message.content }];
  }
  return message.content;
}

function chatMessageMetadataRecord(value: unknown): Record<string, unknown> | undefined {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : undefined;
}

const CANONICAL_SAVED_USER_MESSAGE_ID = /^message:[a-f0-9]{64}$/;
const STABLE_OWNER_TURN_ID = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$/;

function canonicalOwnerTurnId(message: ChatMessage): string | undefined {
  const turnId = message.metadata?.turn_id;
  return message.role === "user"
    && CANONICAL_SAVED_USER_MESSAGE_ID.test(message.id)
    && typeof message.conversation_id === "string"
    && STABLE_OWNER_TURN_ID.test(message.conversation_id)
    && typeof turnId === "string"
    && STABLE_OWNER_TURN_ID.test(turnId)
    ? turnId
    : undefined;
}

export function chatMessageToUiMessage(message: ChatMessage, profile?: ModelProfile | null): ChatUiMessage {
  const isUser = message.role === "user";
  const metadata = message.metadata ?? {};
  const thinking = metadata.thinking as Record<string, unknown> | undefined;
  const timing = metadata.timing as Record<string, unknown> | undefined;
  const pendingApproval = metadata.pending_approval;
  const pendingAuthorityApproval = chatMessageMetadataRecord(metadata.pendingAuthorityApproval ?? metadata.pending_authority_approval);
  const authorityFollowup = chatMessageMetadataRecord(metadata.authority_followup ?? metadata.authorityFollowup);
  const chatDisplay = chatMessageMetadataRecord(metadata.chat_display ?? metadata.chatDisplay);
  const promptUsage = metadata.prompt_usage && typeof metadata.prompt_usage === "object" && !Array.isArray(metadata.prompt_usage)
    ? metadata.prompt_usage as NonNullable<ChatUiMessage["metadata"]>["promptUsage"]
    : undefined;
  const attachedToolCount = Number(metadata.attached_tool_count ?? 0);
  const thinkingDuration = String(timing?.thinking_duration_label ?? "")
    || boundedDurationLabel(timing?.thinking_started_at, timing?.completed_at);
  const displayMetadata = {
    ...(authorityFollowup ? { authorityFollowup } : {}),
    ...(chatDisplay ? { chatDisplay } : {}),
  };
  const explicitMentions = normalizeComposerMentionMetadata(metadata.mentions);
  const fallbackMentions = explicitMentions.length === 0 && Array.isArray(metadata.dropped_widgets)
    ? composerMentionMetadataFromWidgets(metadata.dropped_widgets as DroppedWidget[])
    : [];
  const ownerTurnId = canonicalOwnerTurnId(message);
  const baseMentions = (explicitMentions.length > 0 ? explicitMentions : fallbackMentions)
    .filter((mention) => mention.kind !== "chat" && mention.kind !== "group");
  // History rows are persisted server display snapshots. They do not create
  // Profile ownership, semantic draft selections, or execution permissions.
  const storedReferences = ownerTurnId ? savedChatReferenceMentions(metadata.chat_references) : [];
  const seenMentions = new Set<string>();
  const mentions = [...baseMentions, ...storedReferences].filter((mention) => {
    const key = JSON.stringify([mention.kind, mention.id]);
    if (seenMentions.has(key)) return false;
    seenMentions.add(key);
    return true;
  });
  const userMetadata = Object.keys(displayMetadata).length > 0 || mentions.length > 0 || ownerTurnId
    ? {
        ...displayMetadata,
        ...(mentions.length > 0 ? { mentions } : {}),
        ...(ownerTurnId ? { turn_id: ownerTurnId } : {}),
      }
    : undefined;
  return {
    id: message.id,
    conversationId: message.conversation_id,
    createdAt: message.created_at,
    role: isUser ? "user" : "agent",
    content: normalizeBlocks(message),
    rawText: messageToText(message),
    widget: message.widget,
    events: message.events ?? [],
    toolLogs: message.tool_logs ?? [],
    metadata: isUser
      ? userMetadata
      : {
          executionTime: formatRelativeTime(message.created_at),
          modelName: profile?.display_name ?? String(message.model ?? ""),
          thinkingLabel: String(thinking?.state ?? ""),
          thinkingDuration,
          thinkingTranscript: String(thinking?.transcript ?? ""),
          interrupted: metadata.interrupted === true || message.finish_reason === "interrupted",
          interruptionReason: String(metadata.interruption_reason ?? ""),
          attachedToolCount,
          pendingApproval: pendingApproval && typeof pendingApproval === "object" && !Array.isArray(pendingApproval)
            ? pendingApproval as Record<string, unknown>
            : undefined,
          pendingAuthorityApproval,
          ...displayMetadata,
          promptUsage,
        },
  };
}
