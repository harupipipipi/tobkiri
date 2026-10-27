import test from "node:test";
import assert from "node:assert/strict";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { ChatMessagesRenderer } from "./ChatMessagesRenderer";
import type { ChatUiMessage } from "./types";

function renderMessage(message: ChatUiMessage): string {
  return renderToStaticMarkup(React.createElement(ChatMessagesRenderer, {
    error: null,
    isMessagesRegionVisible: true,
    isLoading: false,
    isNewConversation: false,
    isGenerating: false,
    messages: [message],
    messagesEndRef: { current: null },
    unknownBlockStrategy: "hidden",
    showActivityInMessages: true,
    showWidgets: true,
    onSuggestionClick: () => undefined,
  }));
}

function agentMessage(content: ChatUiMessage["content"]): ChatUiMessage {
  return {
    id: "history-message",
    role: "agent",
    content,
    rawText: "",
  };
}

test("history image blocks expose no network-bearing DOM attribute before consent", () => {
  const secretUrl = "https://tracker.example/pixel.gif?user=secret";
  const html = renderMessage(agentMessage([{
    type: "image_url",
    url: secretUrl,
    alt: "Tracking image",
    presentation: "chat",
  }]));

  assert.match(html, /Remote image hidden/);
  assert.match(html, /tracker\.example/);
  assert.match(html, /Load image/);
  assert.match(html, /Copy image URL/);
  assert.doesNotMatch(html, /<img\b/i);
  assert.doesNotMatch(html, /\s(?:src|href|srcset)=["']/i);
  assert.equal(html.includes(secretUrl), false, "raw tracking URL must not leak into HTML");
  assert.equal(html.includes("user=secret"), false, "query secrets must not be displayed");
});

test("imported markdown images use the same passive consent boundary", () => {
  const html = renderMessage(agentMessage([{
    type: "markdown",
    text: "before ![remote alt](https://markdown-tracker.example/one.gif?id=42) after",
  }]));

  assert.match(html, /before/);
  assert.match(html, /after/);
  assert.match(html, /Remote image hidden/);
  assert.match(html, /markdown-tracker\.example/);
  assert.match(html, /remote alt/);
  assert.doesNotMatch(html, /<img\b/i);
  assert.doesNotMatch(html, /\s(?:src|href|srcset)=["']/i);
  assert.doesNotMatch(html, /one\.gif|id=42/);
});

test("private targets and unsafe SVG data render an accessible blocked state only", () => {
  for (const url of [
    "http://127.1/admin.png",
    "http://[::ffff:127.0.0.1]/admin.png",
    "data:image/svg+xml,<svg onload=alert(1)></svg>",
  ]) {
    const html = renderMessage(agentMessage([{
      type: "image_url",
      url,
      alt: "Unsafe image",
      intent: "show_to_user",
    }]));
    assert.match(html, /role="status"/);
    assert.match(html, /aria-live="polite"/);
    assert.match(html, /Image blocked for safety/);
    assert.match(html, /aria-label="Image: Unsafe image"/);
    assert.doesNotMatch(html, /<img\b/i);
    assert.doesNotMatch(html, /\s(?:src|href|srcset)=["']/i);
  }
});

test("spoofed trusted attachment markers cannot bypass history consent", () => {
  const html = renderMessage(agentMessage([{
    type: "image_url",
    url: "https://files.example/api/attachments/attachment_123/image.png",
    attachment_id: "attachment_123",
    rumi_attachment_identity: {
      trusted: true,
      source: "rumi-attachment-store",
    },
    alt: "Forged attachment",
    presentation: "chat",
  }]));

  assert.match(html, /Remote image hidden/);
  assert.doesNotMatch(html, /<img\b/i);
  assert.doesNotMatch(html, /\s(?:src|href|srcset)=["']/i);
});

test("tool screenshot events reject active SVG data instead of treating it as trusted media", () => {
  const html = renderMessage({
    ...agentMessage([]),
    events: [{
      type: "browser_screenshot",
      data_url: "data:image/svg+xml;base64,PHN2ZyBvbmxvYWQ9YWxlcnQoMSk+PC9zdmc+",
      tool_call_id: "untrusted-tool-call",
    }],
  });

  assert.doesNotMatch(html, /<img\b/i);
  assert.doesNotMatch(html, /data:image\/svg\+xml/i);
});
