import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { renderToStaticMarkup } from "react-dom/server";

import {
  ConversationV4View,
  conversationV4AssistantText,
  conversationV4CapabilityPayload,
  isConversationV4Contribution,
  type ConversationV4Message,
} from "./ConversationV4View";
import type {
  FrontendCapabilityClient,
  VerifiedFrontendContribution,
} from "./frontendContracts";

const contribution: VerifiedFrontendContribution = {
  contribution_id: "defaults.conversation.complete",
  kind: "route",
  mode: "declarative",
  label: "Tobkiri Conversation",
  priority: 100,
  owner_pack_id: "defaultspack",
  owner_pack_hash: `sha256:${"1".repeat(64)}`,
  build_identity: "defaultspack.conversation",
  resolved_profile_revision: "profile-1",
  resolved_plan_hash: "plan-1",
  descriptor_hash: `sha256:${"2".repeat(64)}`,
  route: "/chat",
  action_contract: "conversation.turn.v1",
  view: { type: "conversation_v4" },
  localization: {},
  accessibility: { name: "Tobkiri Conversation", keyboard: true },
};

const capabilities: FrontendCapabilityClient = {
  invokeAction: async () => ({ content: [{ type: "text", text: "Ready" }] }),
  readDataSource: async () => ({ ok: true }),
};

test("ConversationV4View submits only the raw complete-contract transcript", () => {
  const messages: ConversationV4Message[] = [
    { id: "user-1", role: "user", content: "Hello" },
    { id: "assistant-1", role: "assistant", content: "Hi there" },
  ];

  const payload = conversationV4CapabilityPayload(messages);
  assert.deepEqual(payload, {
    messages: [
      { role: "user", content: "Hello" },
      { role: "assistant", content: "Hi there" },
    ],
  });
  assert.equal("operation" in payload, false);
  assert.equal("input" in payload, false);
});

test("ConversationV4View accepts the projected non-streaming completion", () => {
  assert.equal(
    conversationV4AssistantText({
      content: [{ type: "text", text: "A complete-only reply" }],
    }),
    "A complete-only reply",
  );
  assert.equal(conversationV4AssistantText({ content: [] }), null);
});

test("ConversationV4View is selected only by the exact defaultspack chat contribution", () => {
  assert.equal(isConversationV4Contribution(contribution), true);
  assert.equal(isConversationV4Contribution({ ...contribution, route: "/packs" }), false);
  assert.equal(isConversationV4Contribution({ ...contribution, contribution_id: "other" }), false);
  assert.equal(isConversationV4Contribution({ ...contribution, owner_pack_id: "other" }), false);
  assert.equal(isConversationV4Contribution({ ...contribution, build_identity: "other" }), false);
  assert.equal(isConversationV4Contribution({ ...contribution, action_contract: "rumi.action.other.v1" }), false);
});

test("ConversationV4View provides an accessible transcript and composer without legacy chat transport", async () => {
  const markup = renderToStaticMarkup(
    <ConversationV4View
      item={contribution}
      catalogHash={`sha256:${"3".repeat(64)}`}
      capabilities={capabilities}
    />,
  );
  const source = await readFile(new URL("./ConversationV4View.tsx", import.meta.url), "utf8");

  assert.match(markup, /data-conversation-surface="v4"/);
  assert.match(markup, /aria-label="Conversation transcript"/);
  assert.match(markup, /Message Tobkiri/);
  assert.match(markup, /<textarea/);
  assert.match(markup, />Send</);
  assert.doesNotMatch(source, /api\/chat|defaultspackApiFetch|\bfetch\s*\(/);
});
