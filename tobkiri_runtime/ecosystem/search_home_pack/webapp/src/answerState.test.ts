import assert from "node:assert/strict";
import test from "node:test";

import { conversationHref, normalizeAnswerResponse } from "./answerState";

test("normalizes successful answer and privacy-safe conversation link", () => {
  const result = normalizeAnswerResponse({ status: "ok", answer: "Safe answer", model: "provider/model", conversation_id: "chat 1", used_tools: ["web_search"] });
  assert.equal(result.kind, "success");
  assert.equal(result.answer, "Safe answer");
  assert.equal(result.usedToolsCount, 1);
  assert.equal(conversationHref(result.conversationId), "/chat?chat=chat%201");
});

test("distinguishes structured error, empty, malformed and partial payloads", () => {
  assert.equal(normalizeAnswerResponse({ status: "error", error: { message: "Unavailable" } }).kind, "structured-error");
  assert.equal(normalizeAnswerResponse({ status: "ok", answer: " " }).kind, "empty");
  assert.equal(normalizeAnswerResponse({ status: "future", answer: "x" }).kind, "malformed");
  assert.equal(normalizeAnswerResponse({ status: "ok", answer: "kept", interrupted: true }).kind, "partial");
});

test("surfaces degraded tool capability without exposing tool ids as primary copy", () => {
  const result = normalizeAnswerResponse({ status: "ok", answer: "Answer", used_tools: ["internal.secret.tool"], tool_calling_unavailable_reason: "model does not support tools" });
  assert.equal(result.usedToolsCount, 1);
  assert.equal(result.degradedReason, "model does not support tools");
  assert.equal(result.answer.includes("internal.secret.tool"), false);
});

test("malformed service errors cannot become an unrenderable result heading", () => {
  const result = normalizeAnswerResponse({ status: "error", error: { message: { internal: "payload" } } });
  assert.equal(result.kind, "structured-error");
  assert.equal(typeof result.message, "string");
  assert.equal(result.message, "回答のリクエストに失敗しました。");
});
