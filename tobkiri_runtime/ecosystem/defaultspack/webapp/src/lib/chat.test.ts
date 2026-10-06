import test from "node:test";
import assert from "node:assert/strict";
import {
  contentBlocksToText,
  deriveConversationTitle,
  formatRelativeTime,
  inspectConversationIntegrity,
  messageToText,
  orderConversationMessages,
} from "./chat";
import type { ChatMessage } from "./api";
import { chatRetryEligible, type ChatRetryContext, type ChatRetryOwnership } from "./chatRetry";
import { SavedTurnViewFence } from "./optimisticSavedTurn";


test("contentBlocksToText flattens text blocks", () => {
  const text = contentBlocksToText([
    { type: "text", text: "hello" },
    { type: "image", url: "ignored" },
    { type: "text", text: "world" },
  ]);
  assert.equal(text, "hello\nworld");
});

test("messageToText prefers raw_text when present", () => {
  const message = {
    id: "m1",
    role: "assistant",
    content: [{ type: "text", text: "fallback" }],
    raw_text: "preferred",
    created_at: Date.now(),
    conversation_id: "c1",
  } satisfies ChatMessage;
  assert.equal(messageToText(message), "preferred");
});

test("deriveConversationTitle trims and caps length", () => {
  assert.equal(deriveConversationTitle("   "), "New Conversation");
  assert.equal(
    deriveConversationTitle(
      "this is a very long conversation title that should be shortened",
    ),
    "this is a very long conversation title t...",
  );
});

test("formatRelativeTime formats short durations", () => {
  const now = 10_000;
  assert.equal(formatRelativeTime(9_000, now), "just now");
  assert.equal(formatRelativeTime(0, 61_000), "1m ago");
});

test("orderConversationMessages restores chronological sequence and removes duplicate finals", () => {
  const user = {
    id: "user-1",
    role: "user",
    content: [{ type: "text", text: "weather" }],
    raw_text: "weather",
    created_at: 1000,
    conversation_id: "c1",
    sequence_number: 1,
  } satisfies ChatMessage;
  const assistant = {
    id: "assistant-1",
    role: "assistant",
    content: [{ type: "text", text: "searched" }],
    raw_text: "searched",
    created_at: 1010,
    conversation_id: "c1",
    sequence_number: 2,
    events: [{ type: "tool_call_completed", tool_name: "browser_use" }],
  } satisfies ChatMessage;
  const duplicateDone = {
    ...assistant,
    events: [{ type: "tool_call_completed", tool_name: "browser_use", message: "done" }],
  } satisfies ChatMessage;

  const ordered = orderConversationMessages([assistant, user, duplicateDone]);

  assert.deepEqual(ordered.map((message) => message.id), ["user-1", "assistant-1"]);
  assert.deepEqual(ordered[1].events, duplicateDone.events);
});

test("orderConversationMessages collapses duplicate sequence finals even when ids differ", () => {
  const assistantA = {
    id: "assistant-1",
    role: "assistant",
    content: [{ type: "text", text: "draft" }],
    raw_text: "draft",
    created_at: 1010,
    conversation_id: "c1",
    sequence_number: 2,
  } satisfies ChatMessage;
  const assistantB = {
    ...assistantA,
    id: "assistant-2",
    raw_text: "final",
    content: [{ type: "text", text: "final" }],
  } satisfies ChatMessage;

  const ordered = orderConversationMessages([assistantA, assistantB]);
  const diagnostics = inspectConversationIntegrity([assistantA, assistantB]);

  assert.equal(ordered.length, 1);
  assert.equal(ordered[0]?.raw_text, "final");
  assert.equal(diagnostics.collapsedCount, 1);
  assert.equal(diagnostics.duplicateSequenceCount, 1);
});

test("orderConversationMessages keeps canonical durable history when legacy sequence aliases repeat", () => {
  const durableRecords: Array<[number, ChatMessage["role"], string]> = [
    [0, "user", "最初の質問"],
    [1, "assistant", "最初の回答"],
    [2, "user", "GUIDANCE_OK を続けてください"],
    [3, "assistant", "GUIDANCE_OK"],
    [4, "user", "7 + 7 は？"],
    [5, "assistant", "14"],
  ];
  const messages = durableRecords.map(([sequence, role, rawText]) => ({
    id: `durable-${sequence}`,
    role,
    content: [{ type: "text", text: rawText }],
    raw_text: rawText,
    created_at: 10_000 - sequence,
    conversation_id: "native-gemma-conversation",
    sequence,
    sequence_number: 1,
  } satisfies ChatMessage));

  const ordered = orderConversationMessages([...messages].reverse());
  const diagnostics = inspectConversationIntegrity(messages);

  assert.deepEqual(ordered.map((message) => message.id), [
    "durable-0",
    "durable-1",
    "durable-2",
    "durable-3",
    "durable-4",
    "durable-5",
  ]);
  assert.deepEqual(ordered.map((message) => message.role), [
    "user",
    "assistant",
    "user",
    "assistant",
    "user",
    "assistant",
  ]);
  assert.deepEqual(ordered.map((message) => message.raw_text), [
    "最初の質問",
    "最初の回答",
    "GUIDANCE_OK を続けてください",
    "GUIDANCE_OK",
    "7 + 7 は？",
    "14",
  ]);
  assert.equal(diagnostics.collapsedCount, 0);
});

test("orderConversationMessages falls back to legacy aliases when canonical sequence is null", () => {
  const first = {
    id: "legacy-first",
    role: "assistant",
    content: [{ type: "text", text: "first" }],
    raw_text: "first",
    created_at: 1020,
    conversation_id: "c1",
    sequence: null,
    sequence_number: 1,
  } as unknown as ChatMessage;
  const second = {
    id: "legacy-second",
    role: "assistant",
    content: [{ type: "text", text: "second" }],
    raw_text: "second",
    created_at: 1010,
    conversation_id: "c1",
    sequence: null,
    sequence_number: 2,
  } as unknown as ChatMessage;

  const ordered = orderConversationMessages([second, first]);
  const diagnostics = inspectConversationIntegrity([second, first]);

  assert.deepEqual(ordered.map((message) => message.id), ["legacy-first", "legacy-second"]);
  assert.equal(diagnostics.collapsedCount, 0);
});

test("orderConversationMessages collapses canonical duplicate finals with distinct ids", () => {
  const draft = {
    id: "assistant-draft",
    role: "assistant",
    content: [{ type: "text", text: "draft" }],
    raw_text: "draft",
    created_at: 1010,
    conversation_id: "c1",
    sequence: 1,
    sequence_number: 1,
  } satisfies ChatMessage;
  const final = {
    ...draft,
    id: "assistant-final",
    content: [{ type: "text", text: "final" }],
    raw_text: "final",
    sequence_number: 99,
  } satisfies ChatMessage;

  const ordered = orderConversationMessages([draft, final]);
  const diagnostics = inspectConversationIntegrity([draft, final]);

  assert.equal(ordered.length, 1);
  assert.equal(ordered[0]?.raw_text, "final");
  assert.equal(diagnostics.duplicateSequenceCount, 1);
});

const retry: ChatRetryOwnership = {
  profileId: "defaults",
  errorMessage: "Load failed",
  errorGeneration: 4,
  storeId: "store-a",
  viewTicket: { workspaceTabId: "tab-a", conversationId: "conversation-a", epoch: 7 },
};
const context: ChatRetryContext = {
  profileId: "defaults",
  conversationId: "conversation-a",
  error: "Load failed",
  errorGeneration: 4,
  storeId: "store-a",
  viewTicket: { ...retry.viewTicket },
  isGenerating: false,
  hasPendingSavedTurn: false,
  hasActiveSubmission: false,
};

test("fresh retry remains eligible only for the exact error and owner", () => {
  assert.equal(chatRetryEligible(retry, context), true);
  assert.equal(chatRetryEligible(null, context), false);
  assert.equal(chatRetryEligible({ ...retry, storeId: null }, { ...context, storeId: null }), false);
  const denied: Partial<ChatRetryContext>[] = [
    { error: null },
    { error: "Another failure" },
    { errorGeneration: 5 },
    { storeId: null },
    { storeId: "store-b" },
    { profileId: "another-profile" },
    { conversationId: "conversation-b" },
    { viewTicket: { ...context.viewTicket, epoch: 8 } },
    { viewTicket: { ...context.viewTicket, workspaceTabId: "tab-b" } },
    { viewTicket: { ...context.viewTicket, conversationId: "conversation-b" } },
    { isGenerating: true },
    { hasPendingSavedTurn: true },
    { hasActiveSubmission: true },
  ];
  for (const patch of denied) {
    assert.equal(chatRetryEligible(retry, { ...context, ...patch }), false, JSON.stringify(patch));
  }
});

test("returning to the same conversation cannot revive retry from an earlier epoch", () => {
  const fence = new SavedTurnViewFence("tab-a", "conversation-a");
  const ownedRetry = { ...retry, viewTicket: fence.capture() };
  assert.equal(chatRetryEligible(ownedRetry, { ...context, viewTicket: fence.capture() }), true);
  fence.synchronize("tab-b", "conversation-b");
  assert.equal(chatRetryEligible(ownedRetry, { ...context, viewTicket: fence.capture() }), false);
  fence.synchronize("tab-a", "conversation-a");
  assert.equal(chatRetryEligible(ownedRetry, { ...context, viewTicket: fence.capture() }), false);
});

test("synchronous error consumption rejects a repeated stale retry handler", () => {
  let generation = context.errorGeneration;
  assert.equal(chatRetryEligible(retry, { ...context, errorGeneration: generation }), true);
  generation += 1;
  // Even if React has not committed the cleared error or retry state yet.
  assert.equal(chatRetryEligible(retry, { ...context, errorGeneration: generation }), false);
  // A later unrelated error with identical wording still cannot revive it.
  generation += 1;
  assert.equal(chatRetryEligible(retry, { ...context, errorGeneration: generation }), false);
});

test("new-conversation pre-start retry can own a null conversation without a null store", () => {
  const viewTicket = { ...retry.viewTicket, conversationId: null };
  assert.equal(chatRetryEligible({ ...retry, viewTicket }, { ...context, viewTicket, conversationId: null }), true);
});

test("a pre-start failure after own conversation adoption keeps the adopted owner", () => {
  const fence = new SavedTurnViewFence("tab-a", null);
  const adopted = fence.adoptConversation(fence.capture(), "conversation-a");
  assert.ok(adopted);
  const ownedRetry = { ...retry, viewTicket: adopted };
  // React publishing the adopted conversation must not create a fresh epoch.
  assert.equal(fence.synchronize("tab-a", "conversation-a"), false);
  assert.equal(chatRetryEligible(ownedRetry, { ...context, viewTicket: fence.capture() }), true);
});
