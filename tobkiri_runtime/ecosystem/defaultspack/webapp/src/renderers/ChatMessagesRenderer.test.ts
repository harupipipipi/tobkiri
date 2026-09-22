import test from "node:test";
import assert from "node:assert/strict";
import React, { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { AUTHORITY_FOLLOWUP_TEXT, ChatMessagesRenderer, compactLogPreviewText, formatMessageTimestamp, hasRunningToolActivityGroups, isAuthorityWaitingMessage, isCompactLogLikeMessageText, isHiddenAuthorityFollowupMessage, messageCopyText, previewableToolActivityKeys, sanitizeAssistantAuthorityBoilerplate, shouldRenderImageBlockInChat, shouldShowEmptyResponseWarning, streamedBrowserScreenshots, summarizePendingToolNames, summarizeToolActivityGroups, taskDurationForMessage, toolActivityPreviewId, visibleChatMessages } from "./ChatMessagesRenderer";
import type { ChatUiMessage } from "./types";

const RISKY_AUTHORITY_FOLLOWUP_PHRASES = [
  "Thank you for granting",
  "approved provider",
  "approved model",
  "I can now use",
  "使用を許可しました",
];

function message(overrides: Partial<ChatUiMessage>): ChatUiMessage {
  return {
    id: "message-1",
    role: "agent",
    content: [],
    rawText: "",
    ...overrides,
  };
}

function assertNoRiskyAuthorityFollowupPhrases(text: string): void {
  for (const phrase of RISKY_AUTHORITY_FOLLOWUP_PHRASES) {
    assert.equal(text.includes(phrase), false, `unexpected risky phrase: ${phrase}`);
  }
}

test("message copy text includes visible text and code blocks", () => {
  assert.equal(messageCopyText(message({
    content: [
      { type: "markdown", text: "hello" },
      { type: "code", text: "const ok = true;" },
    ],
  })), "hello\n\nconst ok = true;");
});

test("message copy text falls back to raw text", () => {
  assert.equal(messageCopyText(message({ rawText: "fallback text" })), "fallback text");
});

test("unknown blocks fail closed in DOM and copy for legacy strategies", () => {
  const unknown = {
    type: "provider.future",
    token: "never-render-token",
    tool_arguments: { path: "/private/work", secret: "never-render-secret" },
    hidden_reasoning: "never-render-reasoning",
  };
  for (const unknownBlockStrategy of ["hidden", "text", "json", "placeholder"]) {
    const unsafeMessage = message({ content: [unknown] });
    const html = renderToStaticMarkup(createElement(ChatMessagesRenderer, {
      error: null, isMessagesRegionVisible: true, isLoading: false,
      isNewConversation: false, isGenerating: false, messages: [unsafeMessage],
      messagesEndRef: { current: null }, unknownBlockStrategy,
      showActivityInMessages: true, showWidgets: true, onSuggestionClick: () => undefined,
    }));
    assert.match(html, /data-testid="unsupported-chat-block"/);
    assert.match(html, /この内容は現在のRumiでは表示できません/);
    assert.doesNotMatch(html, /never-render|private\/work|hidden_reasoning|tool_arguments/);
    assert.equal(messageCopyText(unsafeMessage), "");
  }
});

test("debug unknown block disclosure is explicit, bounded, and value-free", () => {
  const html = renderToStaticMarkup(createElement(ChatMessagesRenderer, {
    error: null, isMessagesRegionVisible: true, isLoading: false,
    isNewConversation: false, isGenerating: false,
    messages: [message({ content: [{ type: "future.v3", version: "3", status: "secret-looking-value", token: "never-render-token" }] })],
    messagesEndRef: { current: null }, unknownBlockStrategy: "debug",
    showActivityInMessages: true, showWidgets: true, onSuggestionClick: () => undefined,
  }));
  assert.match(html, /開発者向けの制限済み情報/);
  assert.match(html, /rumi.chat.public.v1/);
  assert.doesNotMatch(html, /secret-looking-value|never-render-token/);
});

test("user messages restore human mention badges from semantic metadata", () => {
  const html = renderToStaticMarkup(createElement(ChatMessagesRenderer, {
    error: null,
    isMessagesRegionVisible: true,
    isLoading: false,
    isNewConversation: false,
    isGenerating: false,
    messages: [message({
      id: "user-with-mention",
      role: "user",
      content: [{ type: "text", text: "Use @Browser Computer" }],
      rawText: "Use @Browser Computer",
      metadata: {
        mentions: [{
          id: "browser_computer",
          kind: "tool",
          label: "Browser Computer",
          syntax: "@Browser Computer",
        }],
      },
    })],
    messagesEndRef: { current: null },
    unknownBlockStrategy: "hidden",
    showActivityInMessages: true,
    showWidgets: true,
    onSuggestionClick: () => undefined,
  }));

  assert.match(html, /data-testid="message-mention-badge"/);
  assert.match(html, /@Browser Computer/);
  assert.doesNotMatch(html, />@browser_computer</);
});

test("repository evidence widget renders trusted exact statistics", () => {
  const html = renderToStaticMarkup(createElement(ChatMessagesRenderer, {
    error: null,
    isMessagesRegionVisible: true,
    isLoading: false,
    isNewConversation: false,
    isGenerating: false,
    messages: [message({
      widget: {
        type: "repository_evidence",
        statistics: { files_selected: 7, files_excluded: 93 },
        excluded_reason_counts: {
          secret_like_path: 3,
          utility_model_not_selected: 90,
        },
        excluded_sample: [{ path: "never-render.ts", reason: "sample" }],
      },
    })],
    messagesEndRef: { current: null },
    unknownBlockStrategy: "hidden",
    showActivityInMessages: true,
    showWidgets: true,
    onSuggestionClick: () => undefined,
  }));

  assert.match(html, /data-testid="repository-evidence-widget"/);
  assert.match(html, />93</);
  assert.match(html, /utility_model_not_selected/);
  assert.doesNotMatch(html, /never-render\.ts/);
});

test("markdown links render as destination-aware review controls", () => {
  const html = renderToStaticMarkup(createElement(ChatMessagesRenderer, {
    error: null,
    isMessagesRegionVisible: true,
    isLoading: false,
    isNewConversation: false,
    isGenerating: false,
    messages: [message({
      role: "agent",
      content: [{ type: "markdown", text: "[Account portal](https://example.com/account)" }],
      rawText: "[Account portal](https://example.com/account)",
    })],
    messagesEndRef: { current: null },
    unknownBlockStrategy: "hidden",
    showActivityInMessages: true,
    showWidgets: true,
    onSuggestionClick: () => undefined,
  }));

  assert.match(html, /<button[^>]+aria-label="Account portal; destination example\.com; web"/);
  assert.doesNotMatch(html, /<a[^>]+href="https:\/\/example\.com/);
});

test("assistant authority retry boilerplate is stripped while preserving the answer", () => {
  const leakedText = "The model/API authority is now approved. Retrying the request to DeepSeek V4 Flash via OpenCode Go with the provided credentials and network context.\n\n---\n\nHello! 😊 How can I help you today? I’m DeepSeek V4 Flash, ready to assist you...";
  const answer = "Hello! 😊 How can I help you today? I’m DeepSeek V4 Flash, ready to assist you...";

  assert.equal(sanitizeAssistantAuthorityBoilerplate(leakedText), answer);
  assert.equal(messageCopyText(message({ rawText: leakedText })), answer);
  assert.equal(messageCopyText(message({ content: [{ type: "markdown", text: leakedText }] })), answer);
});

test("assistant authority thank-you boilerplate is stripped while preserving the answer", () => {
  const leakedText = "Thank you for granting the model authority request. I can now use the approved provider...\n\n---\n\nHere is the implementation detail you asked for.";
  const answer = "Here is the implementation detail you asked for.";

  assert.equal(sanitizeAssistantAuthorityBoilerplate(leakedText), answer);
  assert.equal(messageCopyText(message({ content: [{ type: "text", text: leakedText }] })), answer);
});

test("ordinary assistant messages are not sanitized as authority boilerplate", () => {
  const normalText = "Thank you for granting the docs review enough context.\n\n---\n\nHere is the summary.";

  assert.equal(sanitizeAssistantAuthorityBoilerplate(normalText), normalText);
  assert.equal(messageCopyText(message({ rawText: normalText })), normalText);
});

test("formatMessageTimestamp shows the conversation day and time", () => {
  const label = formatMessageTimestamp(Date.UTC(2026, 5, 4, 3, 5));

  assert.match(label, /2026/);
  assert.match(label, /06/);
  assert.match(label, /04/);
  assert.match(label, /03|12/);
  assert.match(label, /05/);
});

test("pending tool summary shows two names and the remaining count", () => {
  const summary = summarizePendingToolNames(["web_search", "browser", "calendar", "web_search"]);

  assert.deepEqual(summary.visibleNames, ["web_search", "browser"]);
  assert.equal(summary.hiddenCount, 1);
  assert.equal(summary.summary, "web_search、browser、その他 1 個が見込まれました");
});

test("inline pending tool activity renders above the message copy action", () => {
  const html = renderToStaticMarkup(React.createElement(ChatMessagesRenderer, {
    error: null,
    isMessagesRegionVisible: true,
    isLoading: false,
    isNewConversation: false,
    isGenerating: true,
    pendingStatus: "tool",
    pendingToolNames: ["coding_file_list"],
    pendingStartedAt: 10_000,
    pendingToolStartedAt: { coding_file_list: 10_000 },
    messages: [message({
      id: "assistant-running",
      role: "agent",
      rawText: "",
      content: [],
      metadata: { thinkingLabel: "streaming" },
    })],
    messagesEndRef: { current: null },
    unknownBlockStrategy: "hidden",
    showActivityInMessages: true,
    showWidgets: true,
    onSuggestionClick: () => undefined,
  }));

  const pendingIndex = html.indexOf("coding_file_list");
  const copyIndex = html.indexOf('aria-label="コピー"');

  assert.notEqual(pendingIndex, -1);
  assert.notEqual(copyIndex, -1);
  assert.ok(pendingIndex < copyIndex);
});

test("loading activity renders semantic track without bounce dots", () => {
  const html = renderToStaticMarkup(createElement(ChatMessagesRenderer, {
    error: null,
    isMessagesRegionVisible: true,
    isLoading: true,
    isNewConversation: false,
    isGenerating: true,
    pendingStatus: "応答を準備しています",
    pendingToolNames: [],
    pendingStartedAt: Date.now() - 2_000,
    messages: [],
    messagesEndRef: { current: null },
    unknownBlockStrategy: "hidden",
    showActivityInMessages: true,
    showWidgets: true,
    onSuggestionClick: () => undefined,
  }));

  assert.match(html, /role="status"/);
  assert.match(html, /aria-label="応答を準備しています"/);
  assert.match(html, /rumi-loading-bars/);
  assert.match(html, /aria-hidden="true" class="shrink-0 font-mono/);
  assert.doesNotMatch(html, /animate-bounce/);
});

test("completed tool activity summary uses compact work count and elapsed span", () => {
  const summary = summarizeToolActivityGroups([
    {
      id: "files",
      label: "ファイル",
      items: [
        {
          id: "item-1",
          kind: "tool" as const,
          toolName: "coding_file_list",
          folder: "coding/files",
          folderLabel: "ファイル",
          input: "src",
          title: "ファイル / coding_file_list: src",
          detail: "Listed 2 files",
          durationLabel: "3s",
          status: "completed",
          timestamp: 10_000,
          supported: true,
        },
      ],
    },
  ]);

  assert.equal(summary.label, "✓ 1件の作業 · 3s");
  assert.equal(summary.itemCount, 1);
  assert.equal(summary.runningCount, 0);
  assert.equal(summary.failedCount, 0);
});

test("running tool activity summary exposes active work and next action", () => {
  const groups = [
    {
      id: "browser",
      label: "ブラウザ",
      items: [
        {
          id: "item-1",
          kind: "tool" as const,
          toolName: "browser_use",
          folder: "browser",
          folderLabel: "ブラウザ",
          input: "東京 今日の天気",
          title: "ブラウザ / browser_use: 東京 今日の天気",
          detail: "使用中",
          durationLabel: "7s",
          nextAction: "画面の変化を確認します",
          status: "running" as const,
          timestamp: 10_000,
          supported: true,
        },
      ],
    },
  ];

  assert.equal(hasRunningToolActivityGroups(groups), true);
  const summary = summarizeToolActivityGroups(groups);
  assert.equal(summary.label, "作業中 · 1件 · 7s");
  assert.equal(summary.visibleTitle, "ブラウザ / browser_use: 東京 今日の天気");
  assert.equal(summary.nextAction, "画面の変化を確認します");
});

test("task duration is human-friendly while running and after completion", () => {
  const startedAt = Date.UTC(2026, 6, 20, 3, 0, 0);
  const completedAt = startedAt + 125_000;
  const running = taskDurationForMessage(
    message({
      createdAt: startedAt,
      metadata: { thinkingLabel: "streaming" },
    }),
    [],
    startedAt + 65_000,
  );
  const completed = taskDurationForMessage(message({
    events: [
      {
        type: "tool_call_started",
        timestamp: startedAt,
        tool_call_id: "duration-call",
        tool_name: "coding_file_read",
      },
      {
        type: "tool_call_completed",
        timestamp: completedAt,
        tool_call_id: "duration-call",
        tool_name: "coding_file_read",
      },
    ],
  }));

  assert.deepEqual(running, { label: "実行中 1分5秒", running: true });
  assert.deepEqual(completed, { label: "実行時間 2分5秒", running: false });
});

test("assistant header replaces relative timestamps with task duration", () => {
  const html = renderToStaticMarkup(createElement(ChatMessagesRenderer, {
    error: null,
    isMessagesRegionVisible: true,
    isLoading: false,
    isNewConversation: false,
    isGenerating: false,
    messages: [message({
      metadata: {
        executionTime: "just now",
        thinkingDuration: "1m 2s",
      },
      rawText: "done",
      content: [{ type: "text", text: "done" }],
    })],
    messagesEndRef: { current: null },
    unknownBlockStrategy: "hidden",
    showActivityInMessages: true,
    showWidgets: true,
    onSuggestionClick: () => undefined,
  }));

  assert.match(html, /Assistant/);
  assert.match(html, /実行時間 1分2秒/);
  assert.doesNotMatch(html, /just now|thinking 1m 2s/);
});

test("stale streaming metadata on a historical assistant message does not show a running timer", () => {
  const startedAt = Date.UTC(2026, 6, 20, 3, 0, 0);
  const completedAt = startedAt + 45_000;
  const html = renderToStaticMarkup(createElement(ChatMessagesRenderer, {
    error: null,
    isMessagesRegionVisible: true,
    isLoading: false,
    isNewConversation: false,
    isGenerating: false,
    messages: [message({
      createdAt: startedAt,
      metadata: { thinkingLabel: "streaming" },
      events: [{
        type: "tool_call_completed",
        timestamp: completedAt,
        tool_call_id: "stale-stream",
        tool_name: "coding_file_read",
      }],
      rawText: "done",
      content: [{ type: "text", text: "done" }],
    })],
    messagesEndRef: { current: null },
    unknownBlockStrategy: "hidden",
    showActivityInMessages: true,
    showWidgets: true,
    onSuggestionClick: () => undefined,
  }));

  assert.match(html, /実行時間 45秒/);
  assert.doesNotMatch(html, /実行中/);
});

test("expanded tool history retains every event and log entry", () => {
  const startedAt = Date.UTC(2026, 6, 20, 3, 0, 0);
  const eventPaths = Array.from({ length: 12 }, (_, index) => `event-${index}.md`);
  const html = renderToStaticMarkup(createElement(ChatMessagesRenderer, {
    error: null,
    isMessagesRegionVisible: true,
    isLoading: false,
    isNewConversation: false,
    isGenerating: false,
    messages: [message({
      events: eventPaths.map((path, index) => ({
        type: "tool_call_started",
        seq: index + 1,
        timestamp: startedAt + index * 1_000,
        tool_call_id: `event-call-${index}`,
        tool_name: "coding_file_read",
        arguments: { path },
      })),
      toolLogs: [{
        tool_name: "coding_file_read",
        tool_call_id: "log-only-call",
        arguments: { path: "log-only.md" },
        result: { status: "ok", data: { path: "log-only.md", content: "ok" } },
        timestamp: startedAt + 20_000,
      }],
    })],
    messagesEndRef: { current: null },
    unknownBlockStrategy: "hidden",
    showActivityInMessages: true,
    showWidgets: true,
    onSuggestionClick: () => undefined,
  }));

  assert.match(html, /aria-label="ツール履歴"/);
  assert.match(html, /aria-expanded="true"/);
  assert.match(html, /aria-label="ツール履歴の詳細"/);
  for (const path of eventPaths) assert.match(html, new RegExp(path));
  assert.match(html, /log-only\.md/);
  assert.doesNotMatch(html, /前の \d+ 件を表示/);
});

test("empty response warning waits until streaming draft is finalized", () => {
  const streaming = message({ metadata: { thinkingLabel: "streaming" } });
  const running = message({ metadata: { thinkingLabel: "running" } });

  assert.equal(shouldShowEmptyResponseWarning(streaming, false), false);
  assert.equal(shouldShowEmptyResponseWarning(running, false), false);
});

test("empty response warning only appears for finalized agent messages without activity", () => {
  const emptyCompleted = message({ metadata: { thinkingLabel: "completed" } });
  const textCompleted = message({ rawText: "done", metadata: { thinkingLabel: "completed" } });

  assert.equal(shouldShowEmptyResponseWarning(emptyCompleted, false), true);
  assert.equal(shouldShowEmptyResponseWarning(textCompleted, false), false);
  assert.equal(shouldShowEmptyResponseWarning(emptyCompleted, true), false);
});

test("interrupted assistant keeps partial content and shows an incomplete-state notice", () => {
  const html = renderToStaticMarkup(createElement(ChatMessagesRenderer, {
    error: null,
    isMessagesRegionVisible: true,
    isLoading: false,
    isNewConversation: false,
    isGenerating: false,
    messages: [message({
      rawText: "valuable partial answer",
      content: [{ type: "text", text: "valuable partial answer" }],
      metadata: {
        thinkingLabel: "failed",
        interrupted: true,
        interruptionReason: "provider_stream_error",
      },
    })],
    messagesEndRef: { current: null },
    unknownBlockStrategy: "hidden",
    showActivityInMessages: true,
    showWidgets: true,
    onSuggestionClick: () => undefined,
  }));

  assert.match(html, /valuable partial answer/);
  assert.match(html, /応答は途中で中断されました/);
  assert.match(html, /role="status"/);
});

test("retried tool attempts render discard history beside the clean running attempt", () => {
  const html = renderToStaticMarkup(createElement(ChatMessagesRenderer, {
    error: null,
    isMessagesRegionVisible: true,
    isLoading: false,
    isNewConversation: false,
    isGenerating: true,
    messages: [message({
      id: "assistant-retry",
      metadata: { thinkingLabel: "streaming" },
      events: [
        {
          type: "tool_call_started",
          seq: 1,
          timestamp: 1_000,
          tool_call_id: "call_1",
          tool_name: "coding_file_read",
          provider_attempt: 1,
          provider_attempt_generation: 1,
        },
        {
          type: "tool_call_completed",
          seq: 2,
          timestamp: 1_500,
          tool_call_id: "call_1",
          tool_name: "coding_file_read",
          provider_attempt: 1,
          provider_attempt_generation: 1,
          provider_attempt_discarded: true,
          is_error: true,
          display_text: "provider 応答の中断により未実行の tool 入力を破棄しました",
        },
        {
          type: "tool_call_started",
          seq: 4,
          timestamp: 2_000,
          tool_call_id: "call_1",
          tool_name: "coding_file_read",
          provider_attempt: 2,
          provider_attempt_generation: 2,
          arguments: { path: "README.md" },
        },
      ],
    })],
    messagesEndRef: { current: null },
    unknownBlockStrategy: "hidden",
    showActivityInMessages: true,
    showWidgets: true,
    onSuggestionClick: () => undefined,
  }));

  assert.match(html, /未実行の tool 入力を破棄しました/);
  assert.match(html, /README\.md/);
  assert.match(html, /1件失敗/);
  assert.match(html, /作業中/);
});

test("tool previews match retry generations while legacy events still use call ids", () => {
  const keys = previewableToolActivityKeys([
    {
      type: "tool_call_completed",
      tool_call_id: "call_1",
      provider_attempt_generation: 1,
      provider_attempt_discarded: true,
    },
    {
      type: "tool_call_completed",
      tool_call_id: "call_1",
      provider_attempt_generation: 2,
    },
    {
      type: "tool_call_completed",
      tool_call_id: "legacy_call",
    },
  ]);

  assert.equal(toolActivityPreviewId({
    toolCallId: "call_1",
    providerAttemptGeneration: 1,
  }, keys), undefined);
  assert.equal(toolActivityPreviewId({
    toolCallId: "call_1",
    providerAttemptGeneration: 2,
  }, keys), "call_1");
  assert.equal(toolActivityPreviewId({
    toolCallId: "legacy_call",
  }, keys), "legacy_call");
});

test("authority approval followup is hidden while waiting response remains passive", () => {
  assert.equal(AUTHORITY_FOLLOWUP_TEXT, "Internal authority resume.");
  assertNoRiskyAuthorityFollowupPhrases(AUTHORITY_FOLLOWUP_TEXT);

  const waiting = message({
    id: "authority-waiting",
    rawText: "モデル/API の使用許可が必要です。承認後に続行します。",
    content: [{ type: "text", text: "モデル/API の使用許可が必要です。承認後に続行します。" }],
    metadata: {
      pendingAuthorityApproval: {
        request_id: "approval-1",
        permission_id: "model.invoke",
      },
    },
  });
  const followup = message({
    id: "authority-followup",
    role: "user",
    rawText: AUTHORITY_FOLLOWUP_TEXT,
    content: [{ type: "text", text: AUTHORITY_FOLLOWUP_TEXT }],
    metadata: {
      authorityFollowup: {
        request_id: "approval-1",
        permission_id: "model.invoke",
        hidden: true,
      },
      chatDisplay: {
        hidden: true,
        reason: "authority_followup",
      },
    },
  });

  assert.equal(isAuthorityWaitingMessage(waiting), true);
  assert.equal(isHiddenAuthorityFollowupMessage(followup), true);
  assert.deepEqual(visibleChatMessages([waiting, followup]).map((item) => item.id), ["authority-waiting"]);
});

test("authority waiting message is not replaced by the settled assistant continuation", () => {
  assertNoRiskyAuthorityFollowupPhrases(AUTHORITY_FOLLOWUP_TEXT);

  const waiting = message({
    id: "authority-waiting",
    rawText: "モデル/API の使用許可が必要です。承認後に続行します。",
    content: [{ type: "text", text: "モデル/API の使用許可が必要です。承認後に続行します。" }],
    metadata: {
      pendingAuthorityApproval: {
        request_id: "approval-1",
        permission_id: "model.invoke",
      },
    },
  });
  const followup = message({
    id: "authority-followup",
    role: "user",
    rawText: AUTHORITY_FOLLOWUP_TEXT,
    content: [{ type: "text", text: AUTHORITY_FOLLOWUP_TEXT }],
    metadata: {
      authorityFollowup: {
        request_id: "approval-1",
        permission_id: "model.invoke",
        hidden: true,
      },
      chatDisplay: {
        hidden: true,
        reason: "authority_followup",
      },
    },
  });
  const continuation = message({
    id: "authority-continuation",
    rawText: "Hello! How can I assist you today?",
    content: [{ type: "text", text: "Hello! How can I assist you today?" }],
    metadata: { thinkingLabel: "completed" },
  });

  assert.deepEqual(visibleChatMessages([waiting, followup, continuation]).map((item) => item.id), ["authority-waiting", "authority-continuation"]);
});

test("prompt usage disclosure can be hidden from chat messages", () => {
  const promptMessage = message({
    id: "assistant-with-prompts",
    rawText: "done",
    content: [{ type: "text", text: "done" }],
    metadata: {
      promptUsage: {
        active_count: 1,
        token_estimate: { total: 12 },
        segments: [{ id: "prompt:default_chat", label: "default_chat", status: "active", tokens: 12 }],
      },
    },
  });
  const baseProps = {
    error: null,
    isMessagesRegionVisible: true,
    isLoading: false,
    isNewConversation: false,
    isGenerating: false,
    messages: [promptMessage],
    messagesEndRef: { current: null },
    unknownBlockStrategy: "hidden",
    showActivityInMessages: true,
    showWidgets: true,
    onSuggestionClick: () => undefined,
  };

  const visible = renderToStaticMarkup(createElement(ChatMessagesRenderer, baseProps));
  const hidden = renderToStaticMarkup(createElement(ChatMessagesRenderer, {
    ...baseProps,
    showPromptUsageInMessages: false,
  }));

  assert.match(visible, /Prompt used/);
  assert.doesNotMatch(hidden, /Prompt used/);
});

test("long terminal-style output is detected for compact display", () => {
  const logText = JSON.stringify({
    tool_name: "coding_terminal_exec",
    classification: "high",
    risk_reasons: ["shell_escape"],
    cwd: "/tmp/project",
    exit_code: 0,
    stdout: Array.from({ length: 80 }, (_, index) => `pytest line ${index}`).join("\\n"),
    stderr: "",
  }).repeat(8);

  assert.equal(isCompactLogLikeMessageText(logText), true);
});

test("ordinary long markdown is not treated as a terminal log", () => {
  const prose = Array.from({ length: 80 }, (_, index) => (
    `Section ${index}: this paragraph explains architecture, tradeoffs, state transitions, UI behavior, and next steps in normal prose.`
  )).join("\n\n");

  assert.equal(isCompactLogLikeMessageText(prose), false);
});

test("compact log preview keeps head and tail while normalizing escaped newlines", () => {
  const text = `{"stdout":"${Array.from({ length: 420 }, (_, index) => `line-${index}`).join("\\n")}","exit_code":0,"classification":"high","risk_reasons":["shell_escape"],"cwd":"/tmp/project","tool_name":"coding_terminal_exec"}`;
  const preview = compactLogPreviewText(text, 800);

  assert.equal(preview.omitted, true);
  assert.match(preview.text, /line-0/);
  assert.match(preview.text, /line-419/);
  assert.match(preview.text, /chars omitted/);
  assert.equal(preview.text.includes("\\nline-"), false);
});

test("structured message blocks render with horizontal scroll instead of forced wrapping", () => {
  const code = `const fixture = "${"/Users/haru/project/".repeat(16)}";`;
  const html = renderToStaticMarkup(createElement(ChatMessagesRenderer, {
    error: null,
    isMessagesRegionVisible: true,
    isLoading: false,
    isNewConversation: false,
    isGenerating: false,
    messages: [message({
      id: "assistant-code",
      content: [{ type: "code", text: code }],
      rawText: "",
      metadata: { thinkingLabel: "completed" },
    })],
    messagesEndRef: { current: null },
    unknownBlockStrategy: "hidden",
    showActivityInMessages: true,
    showWidgets: true,
    onSuggestionClick: () => undefined,
  }));

  assert.match(
    html,
    /<pre class="max-w-full overflow-x-auto overflow-y-auto whitespace-pre rounded-lg bg-zinc-900 p-3 font-mono text-\[12px\] text-zinc-200">/,
  );
});

test("image blocks stay out of chat unless explicitly marked for display", () => {
  assert.equal(shouldRenderImageBlockInChat({ type: "image_url", url: "data:image/png;base64,abc" }), false);
  assert.equal(shouldRenderImageBlockInChat({ type: "image_url", url: "data:image/png;base64,abc", presentation: "chat" }), true);
  assert.equal(shouldRenderImageBlockInChat({ type: "image", url: "data:image/png;base64,abc", intent: "show_to_user" }), true);
});

test("streamed browser screenshots include explicit screenshot events", () => {
  const screenshots = streamedBrowserScreenshots(message({
    id: "optimistic-assistant-1",
    events: [
      {
        type: "browser_screenshot",
        tool_call_id: "call_1",
        data_url: "data:image/png;base64,abc",
        action: "computer.screenshot",
        image_size: { width: 800, height: 600 },
      },
    ],
  }));

  assert.equal(screenshots.length, 1);
  assert.equal(screenshots[0].data_url, "data:image/png;base64,abc");
  assert.equal(screenshots[0].action, "computer.screenshot");
  assert.deepEqual(screenshots[0].image_size, { width: 800, height: 600 });
});

test("streamed browser screenshots include nested tool result artifacts", () => {
  const screenshots = streamedBrowserScreenshots(message({
    id: "optimistic-assistant-2",
    events: [
      {
        type: "tool_result",
        tool_name: "browser_companion",
        tool_call_id: "call_2",
        result: {
          data: {
            screenshot: {
              data_url: "data:image/png;base64,def",
              marker: { x: 10, y: 12 },
            },
          },
        },
      },
    ],
  }));

  assert.equal(screenshots.length, 1);
  assert.equal(screenshots[0].tool_call_id, "call_2");
  assert.equal(screenshots[0].tool_name, "browser_companion");
  assert.equal(screenshots[0].data_url, "data:image/png;base64,def");
  assert.deepEqual(screenshots[0].marker, { x: 10, y: 12 });
});


test("chat send error exposes retry and dismiss actions without truncating the message", () => {
  const html = renderToStaticMarkup(createElement(ChatMessagesRenderer, {
    error: "Network connection failed while sending a very long Japanese/English message.",
    isMessagesRegionVisible: true,
    isLoading: false,
    isNewConversation: false,
    isGenerating: false,
    messages: [],
    messagesEndRef: { current: null },
    unknownBlockStrategy: "hidden",
    showActivityInMessages: true,
    showWidgets: true,
    onSuggestionClick: () => undefined,
    onRetry: () => undefined,
    onDismissError: () => undefined,
  }));

  assert.match(html, /role="alert"/);
  assert.match(html, />再試行</);
  assert.match(html, /aria-label="エラーを閉じる"/);
  assert.match(html, /Network connection failed/);
});
