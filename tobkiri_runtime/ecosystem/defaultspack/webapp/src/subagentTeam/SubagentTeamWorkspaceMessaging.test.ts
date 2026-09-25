import test from "node:test";
import assert from "node:assert/strict";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { MessageRow, SubagentTeamWorkspace } from "./SubagentTeamWorkspace";

test("preview workspace explains that submitted text is only a local draft", () => {
  const html = renderToStaticMarkup(createElement(SubagentTeamWorkspace));

  assert.match(html, /Preview workspace: submitted text is saved only as a local draft/);
  assert.match(html, /data-testid="subagent-message-input"/);
  assert.doesNotMatch(html, /data-testid="subagent-message-input"[^>]*disabled/);
});

test("failed outgoing message has explicit state and recovery actions", () => {
  const html = renderToStaticMarkup(createElement(MessageRow, {
    message: {
      id: "local-client-1",
      company_id: "company-1",
      channel_id: "ship-room",
      sender_id: "you",
      content: "exact draft",
      metadata: {
        client_message_id: "client-1",
        outgoing_state: "failed",
        outgoing_error: "offline",
      },
    },
    onRetry: () => undefined,
    onEdit: () => undefined,
    onCopy: () => undefined,
    onRemove: () => undefined,
  }));

  assert.match(html, /data-outgoing-state="failed"/);
  assert.match(html, />Failed</);
  assert.match(html, /> Retry</);
  assert.match(html, /> Edit</);
  assert.match(html, /> Copy</);
  assert.match(html, /> Remove</);
});

test("authoritatively committed outgoing message has no recovery actions", () => {
  const html = renderToStaticMarkup(createElement(MessageRow, {
    message: {
      id: "server-1",
      company_id: "company-1",
      channel_id: "ship-room",
      sender_id: "user",
      content: "delivered",
      metadata: { client_message_id: "client-1" },
    },
    onRetry: () => undefined,
    onEdit: () => undefined,
    onCopy: () => undefined,
    onRemove: () => undefined,
  }));

  assert.match(html, /data-outgoing-state="committed"/);
  assert.match(html, />Delivered</);
  assert.doesNotMatch(html, /> Retry|> Edit|> Copy|> Remove/);
});
