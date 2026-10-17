import type { ChatContentBlock, ChatMessage, Conversation } from "./api";

type DedupeDiagnostics = {
  collapsedCount: number;
  duplicateIdCount: number;
  duplicateSequenceCount: number;
  duplicateKeys: string[];
};

export function contentBlocksToText(content: string | ChatContentBlock[]): string {
  if (typeof content === "string") {
    return content;
  }
  return content
    .map((block) => {
      if (typeof block === "string") {
        return block;
      }
      if (block.type === "text") {
        return String(block.text ?? "");
      }
      return "";
    })
    .filter(Boolean)
    .join("\n");
}

export function messageToText(message: ChatMessage): string {
  if (message.raw_text && message.raw_text.trim()) {
    return message.raw_text;
  }
  return contentBlocksToText(message.content).trim();
}

function numericValue(value: unknown): number | null {
  const numberValue = Number(value);
  return Number.isFinite(numberValue) ? numberValue : null;
}

function canonicalSequenceValue(message: ChatMessage): number | null {
  const sequence = message.sequence;
  return typeof sequence === "number" && Number.isSafeInteger(sequence) && sequence >= 0
    ? sequence
    : null;
}

function legacySequenceValue(message: ChatMessage): number | null {
  const sequence = numericValue(message.sequence_number);
  return sequence && sequence > 0 ? sequence : null;
}

function messageSequenceValue(message: ChatMessage): number | null {
  return canonicalSequenceValue(message) ?? legacySequenceValue(message);
}

function messageSortKey(message: ChatMessage, originalIndex: number): {
  sequence: number | null;
  createdAt: number;
  originalIndex: number;
} {
  const sequence = messageSequenceValue(message);
  return {
    sequence,
    createdAt: numericValue(message.created_at) ?? 0,
    originalIndex,
  };
}

function mergeConversationMessage(existing: ChatMessage | undefined, message: ChatMessage): ChatMessage {
  return {
    ...(existing ?? {}),
    ...message,
    events: message.events ?? existing?.events ?? null,
    tool_logs: message.tool_logs ?? existing?.tool_logs ?? null,
  };
}

function dedupeKeysForMessage(message: ChatMessage, index: number): string[] {
  const keys: string[] = [];
  const id = String(message.id || "").trim();
  if (id) keys.push(`id:${id}`);
  const canonicalSequence = canonicalSequenceValue(message);
  const legacySequence = legacySequenceValue(message);
  const role = String(message.role || "").trim();
  const conversationId = String(message.conversation_id || "").trim();
  if (role && conversationId) {
    if (canonicalSequence !== null) {
      keys.push(`seq:${conversationId}:${role}:canonical:${canonicalSequence}`);
    } else if (legacySequence !== null) {
      keys.push(`seq:${conversationId}:${role}:legacy:${legacySequence}`);
    }
  }
  if (keys.length === 0) keys.push(`__message_${index}`);
  return keys;
}

function dedupeConversationMessages(messages: ChatMessage[]): {
  entries: Array<{ message: ChatMessage; originalIndex: number }>;
  diagnostics: DedupeDiagnostics;
} {
  const canonicalByKey = new Map<string, string>();
  const entries = new Map<string, { message: ChatMessage; originalIndex: number }>();
  const duplicateKeys = new Set<string>();
  let duplicateIdCount = 0;
  let duplicateSequenceCount = 0;

  messages.forEach((message, index) => {
    const keys = dedupeKeysForMessage(message, index);
    const canonical = keys
      .map((key) => canonicalByKey.get(key))
      .find((value): value is string => Boolean(value))
      ?? keys[0];
    const existing = entries.get(canonical);
    if (existing) {
      const matchedById = keys.some((key) => key.startsWith("id:") && canonicalByKey.get(key) === canonical);
      const matchedBySequence = !matchedById && keys.some((key) => key.startsWith("seq:") && canonicalByKey.get(key) === canonical);
      if (matchedById) duplicateIdCount += 1;
      else if (matchedBySequence) duplicateSequenceCount += 1;
      duplicateKeys.add(canonical);
    }
    entries.set(canonical, {
      message: mergeConversationMessage(existing?.message, message),
      originalIndex: existing?.originalIndex ?? index,
    });
    keys.forEach((key) => canonicalByKey.set(key, canonical));
  });

  return {
    entries: [...entries.values()],
    diagnostics: {
      collapsedCount: duplicateIdCount + duplicateSequenceCount,
      duplicateIdCount,
      duplicateSequenceCount,
      duplicateKeys: [...duplicateKeys].sort(),
    },
  };
}

export function inspectConversationIntegrity(messages: ChatMessage[]): DedupeDiagnostics {
  return dedupeConversationMessages(messages).diagnostics;
}

export function orderConversationMessages(messages: ChatMessage[]): ChatMessage[] {
  return dedupeConversationMessages(messages).entries
    .sort((left, right) => {
      const leftKey = messageSortKey(left.message, left.originalIndex);
      const rightKey = messageSortKey(right.message, right.originalIndex);
      if (leftKey.sequence !== null && rightKey.sequence !== null && leftKey.sequence !== rightKey.sequence) {
        return leftKey.sequence - rightKey.sequence;
      }
      if (leftKey.createdAt !== rightKey.createdAt) {
        return leftKey.createdAt - rightKey.createdAt;
      }
      return leftKey.originalIndex - rightKey.originalIndex;
    })
    .map((entry) => entry.message);
}

export function deriveConversationTitle(seed: string): string {
  const title = seed.replace(/\s+/g, " ").trim();
  if (!title) {
    return "New Conversation";
  }
  return title.length > 40 ? `${title.slice(0, 40)}...` : title;
}

export function formatRelativeTime(timestamp: number, now = Date.now()): string {
  const diffMs = Math.max(0, now - timestamp);
  const diffMinutes = Math.floor(diffMs / 60000);
  if (diffMinutes < 1) {
    return "just now";
  }
  if (diffMinutes < 60) {
    return `${diffMinutes}m ago`;
  }
  const diffHours = Math.floor(diffMinutes / 60);
  if (diffHours < 24) {
    return `${diffHours}h ago`;
  }
  const diffDays = Math.floor(diffHours / 24);
  if (diffDays < 7) {
    return `${diffDays}d ago`;
  }
  return new Intl.DateTimeFormat("ja-JP", {
    month: "short",
    day: "numeric",
  }).format(timestamp);
}

export function conversationSummary(conversation: Conversation | null): {
  title: string;
  messageCount: number;
  lastMessage: string;
} {
  if (!conversation) {
    return {
      title: "Ready",
      messageCount: 0,
      lastMessage: "まだ会話はありません。",
    };
  }
  const lastMessage = conversation.messages[conversation.messages.length - 1];
  return {
    title: conversation.title,
    messageCount: conversation.messages.length,
    lastMessage: lastMessage ? messageToText(lastMessage) : "まだメッセージはありません。",
  };
}
