import type { ChatContentBlock } from "./api";

export const CHAT_BLOCK_PRESENTATION_SCHEMA = "rumi.chat.public.v1" as const;

export const PUBLIC_CHAT_BLOCK_TYPES = new Set(["text", "markdown", "code", "image", "image_url"]);

const PRIVATE_FIELD_PATTERN = /(?:auth|approval|argument|binary|bytes|content|cookie|credential|data|file|hidden|key|path|payload|reasoning|result|secret|token|url)/i;
const SAFE_FIELD_NAME_PATTERN = /^[a-z][a-z0-9_.-]{0,63}$/i;
const MAX_DIAGNOSTIC_FIELDS = 12;

export type UnknownBlockDiagnostic = {
  presentationSchema: typeof CHAT_BLOCK_PRESENTATION_SCHEMA;
  blockType: string;
  sourceVersion?: string;
  publicFieldNames: string[];
  omittedFieldCount: number;
};

export function isPublicChatBlock(block: unknown): block is ChatContentBlock {
  if (!block || typeof block !== "object" || Array.isArray(block)) return false;
  const type = String((block as ChatContentBlock).type ?? "");
  return PUBLIC_CHAT_BLOCK_TYPES.has(type);
}

export function unknownBlockDiagnostic(block: unknown): UnknownBlockDiagnostic {
  const record = block && typeof block === "object" && !Array.isArray(block)
    ? block as Record<string, unknown>
    : {};
  const rawType = typeof record.type === "string" ? record.type : "";
  // `type` comes from an untrusted block too. It is useful only as a bounded,
  // identifier-like diagnostic label; never reflect arbitrary value text.
  const type = rawType.length <= 80
    && SAFE_FIELD_NAME_PATTERN.test(rawType)
    && !PRIVATE_FIELD_PATTERN.test(rawType)
    ? rawType
    : rawType ? "unknown" : "malformed";
  const sourceVersionValue = record.schema_version ?? record.version;
  const sourceVersion = typeof sourceVersionValue === "string" && /^[a-z0-9_.-]{1,32}$/i.test(sourceVersionValue)
    ? sourceVersionValue
    : undefined;
  const eligibleNames = Object.keys(record)
    .filter((name) => name !== "type" && name !== "schema_version" && name !== "version")
    .filter((name) => SAFE_FIELD_NAME_PATTERN.test(name) && !PRIVATE_FIELD_PATTERN.test(name))
    .sort();
  const publicFieldNames = eligibleNames.slice(0, MAX_DIAGNOSTIC_FIELDS);
  return {
    presentationSchema: CHAT_BLOCK_PRESENTATION_SCHEMA,
    blockType: type,
    ...(sourceVersion ? { sourceVersion } : {}),
    publicFieldNames,
    omittedFieldCount: Math.max(0, Object.keys(record).length - publicFieldNames.length - 1),
  };
}

export function safeUnknownBlockDetails(block: unknown): string {
  return JSON.stringify(unknownBlockDiagnostic(block), null, 2);
}
