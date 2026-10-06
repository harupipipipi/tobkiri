/** Exact provisional public progress wire; no receipt or authority is conveyed. */
export const THREAD_PROGRESS_VERSION = "tobkiri.turn-progress.v1";
export const THREAD_PROGRESS_LIMITS = { pageEvents: 128, pageBytes: 65536, events: 4096, bytes: 4194304, deltaBytes: 16384, ttlMs: 120000 };
export type ThreadProgressBinding = {
  turn_id: string; conversation_id: string; parent_id: string; request_id: string;
  conversation_revision: number; input_digest: string; ai_input_digest: string;
};
export type ThreadProgressEvent = { type: "text_delta" | "thinking_delta"; delta: string }
  | { type: "tool_started"; tool_id: string; tool_call_id: string; arguments: Record<string, unknown> }
  | { type: "tool_completed"; tool_id: string; tool_call_id: string; status: "success" | "error"; content: string }
  | { type: "finish"; finish_reason: "stop" | "length" | "tool_calls" | "end_turn" | "max_tokens" | "tool_use" };
export type ThreadProgressPage = {
  version: typeof THREAD_PROGRESS_VERSION; progress_id?: string; provisional: true; binding: ThreadProgressBinding;
  events: Array<{ cursor: number; event: ThreadProgressEvent }>; cursor: number;
  provider_complete: boolean; expires_at_ms: number;
  canonical_turn_status: "queued" | "running" | "waiting" | "completed" | "failed" | "cancelled" | null;
};
export const progressRecord = (value: unknown): value is Record<string, unknown> =>
  value !== null && typeof value === "object" && !Array.isArray(value);
export const progressId = (value: unknown): value is string => typeof value === "string" && /^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$/.test(value);
export const progressDigest = (value: unknown): value is string => typeof value === "string" && /^sha256:[0-9a-f]{64}$/.test(value);
export const progressStageId = (value: unknown): value is string => typeof value === "string" && /^[a-f0-9]{64}$/.test(value);
export const progressBytes = (value: unknown): number => new TextEncoder().encode(JSON.stringify(value)).length;
const exact = (value: Record<string, unknown>, keys: string[]) => Object.keys(value).length === keys.length
  && keys.every((key) => Object.prototype.hasOwnProperty.call(value, key));
const cursor = (value: unknown) => Number.isSafeInteger(value) && Number(value) >= 0 && Number(value) <= 4096;

/** Reject a whole malformed/oversize/expired page before retaining any delta. */
export function parseThreadProgressPage(value: unknown, now = Date.now()): ThreadProgressPage | null {
  try {
    if (!progressRecord(value) || !exact(value, ["version", "provisional", "binding", "events", "cursor", "provider_complete", "expires_at_ms", "canonical_turn_status", ...("progress_id" in value ? ["progress_id"] : [])])
      || ("progress_id" in value && !progressStageId(value.progress_id))
      || value.version !== THREAD_PROGRESS_VERSION || value.provisional !== true || !cursor(value.cursor)
      || typeof value.provider_complete !== "boolean" || !Number.isSafeInteger(value.expires_at_ms)
      || Number(value.expires_at_ms) <= now || Number(value.expires_at_ms) > now + THREAD_PROGRESS_LIMITS.ttlMs
      || !["queued", "running", "waiting", "completed", "failed", "cancelled", null].includes(value.canonical_turn_status as string | null)
      || progressBytes(value) > THREAD_PROGRESS_LIMITS.pageBytes) return null;
    const binding = value.binding;
    if (!progressRecord(binding) || !exact(binding, ["turn_id", "conversation_id", "parent_id", "request_id", "conversation_revision", "input_digest", "ai_input_digest"])
      || !["turn_id", "conversation_id", "parent_id", "request_id"].every((key) => progressId(binding[key]))
      || !["input_digest", "ai_input_digest"].every((key) => progressDigest(binding[key]))
      || !Number.isSafeInteger(binding.conversation_revision) || Number(binding.conversation_revision) < 0
      || !Array.isArray(value.events) || value.events.length > THREAD_PROGRESS_LIMITS.pageEvents) return null;
    for (const item of value.events) {
      if (!progressRecord(item) || !exact(item, ["cursor", "event"]) || !cursor(item.cursor) || Number(item.cursor) < 1 || !progressRecord(item.event)) return null;
      const event = item.event;
      if (event.type === "text_delta" || event.type === "thinking_delta") {
        if (!exact(event, ["type", "delta"]) || typeof event.delta !== "string"
          || new TextEncoder().encode(event.delta).length > THREAD_PROGRESS_LIMITS.deltaBytes) return null;
      } else if (event.type === "tool_started") {
        if (!progressStageId(value.progress_id) || !exact(event, ["type", "tool_id", "tool_call_id", "arguments"])
          || !progressId(event.tool_id) || !progressId(event.tool_call_id) || !progressRecord(event.arguments)
          || progressBytes(event) > THREAD_PROGRESS_LIMITS.deltaBytes) return null;
      } else if (event.type === "tool_completed") {
        if (!progressStageId(value.progress_id) || !exact(event, ["type", "tool_id", "tool_call_id", "status", "content"])
          || !progressId(event.tool_id) || !progressId(event.tool_call_id)
          || !["success", "error"].includes(String(event.status)) || typeof event.content !== "string"
          || progressBytes(event) > THREAD_PROGRESS_LIMITS.deltaBytes) return null;
        const result = JSON.parse(event.content);
        if (!progressRecord(result) || !exact(result, ["status", "result", "error"])
          || result.status !== event.status) return null;
      } else if (event.type === "finish") {
        if (!exact(event, ["type", "finish_reason"]) || !["stop", "length", "tool_calls", "end_turn", "max_tokens", "tool_use"].includes(String(event.finish_reason))) return null;
      } else return null;
    }
    return JSON.parse(JSON.stringify(value)) as ThreadProgressPage;
  } catch { return null; }
}
