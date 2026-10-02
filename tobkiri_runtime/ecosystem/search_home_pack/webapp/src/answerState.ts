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
    return { kind: "malformed", answer: "", model: "", conversationId: "", usedToolsCount: 0, degradedReason: "", message: "回答データを読み取れませんでした。" };
  }
  const payload = value as SearchAnswerResponse & { status?: string; partial?: boolean; interrupted?: boolean };
  const answer = typeof payload.answer === "string" ? payload.answer.trim() : "";
  const model = typeof payload.model === "string" ? payload.model : "";
  const conversationId = typeof payload.conversation_id === "string" ? payload.conversation_id : "";
  const usedToolsCount = Array.isArray(payload.used_tools) ? payload.used_tools.filter((item) => typeof item === "string" && item).length : 0;
  const degradedReason = typeof payload.tool_calling_unavailable_reason === "string" ? payload.tool_calling_unavailable_reason : "";
  if (payload.status === "error") {
    const message = typeof payload.error?.message === "string" && payload.error.message.trim()
      ? payload.error.message
      : "回答のリクエストに失敗しました。";
    return { kind: "structured-error", answer, model, conversationId, usedToolsCount, degradedReason, message };
  }
  if (payload.status !== "ok") {
    return { kind: "malformed", answer, model, conversationId, usedToolsCount, degradedReason, message: "回答の状態を確認できませんでした。" };
  }
  if (!answer) {
    return { kind: "empty", answer, model, conversationId, usedToolsCount, degradedReason, message: "回答のテキストがありませんでした。" };
  }
  if (payload.partial || payload.interrupted) {
    return { kind: "partial", answer, model, conversationId, usedToolsCount, degradedReason, message: "中断前に届いた回答を表示しています。" };
  }
  return { kind: "success", answer, model, conversationId, usedToolsCount, degradedReason, message: "回答が届きました" };
}

export function conversationHref(conversationId: string): string {
  return conversationId ? `/chat?chat=${encodeURIComponent(conversationId)}` : "";
}
