import type { SearchAnswerResponse } from "./api";

export type AnswerResult = {
  kind: "success" | "partial" | "structured-error" | "empty" | "malformed";
  answer: string;
  model: string;
  conversationId: string;
  usedToolsCount: number;
  degradedReason: string;
  message: string;
};

export function normalizeAnswerResponse(value: unknown): AnswerResult {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return { kind: "malformed", answer: "", model: "", conversationId: "", usedToolsCount: 0, degradedReason: "", message: "The server returned an invalid answer payload." };
  }
  const payload = value as SearchAnswerResponse & { status?: string; partial?: boolean; interrupted?: boolean };
  const answer = typeof payload.answer === "string" ? payload.answer.trim() : "";
  const model = typeof payload.model === "string" ? payload.model : "";
  const conversationId = typeof payload.conversation_id === "string" ? payload.conversation_id : "";
  const usedToolsCount = Array.isArray(payload.used_tools) ? payload.used_tools.filter((item) => typeof item === "string" && item).length : 0;
  const degradedReason = typeof payload.tool_calling_unavailable_reason === "string" ? payload.tool_calling_unavailable_reason : "";
  if (payload.status === "error") {
    return { kind: "structured-error", answer, model, conversationId, usedToolsCount, degradedReason, message: payload.error?.message || "The answer request was rejected." };
  }
  if (payload.status !== "ok") {
    return { kind: "malformed", answer, model, conversationId, usedToolsCount, degradedReason, message: "The server returned an unknown answer status." };
  }
  if (!answer) {
    return { kind: "empty", answer, model, conversationId, usedToolsCount, degradedReason, message: "The request completed without answer text." };
  }
  if (payload.partial || payload.interrupted) {
    return { kind: "partial", answer, model, conversationId, usedToolsCount, degradedReason, message: "Partial answer retained after interruption." };
  }
  return { kind: "success", answer, model, conversationId, usedToolsCount, degradedReason, message: "Answer ready." };
}

export function conversationHref(conversationId: string): string {
  return conversationId ? `/panel?chat=${encodeURIComponent(conversationId)}` : "";
}
