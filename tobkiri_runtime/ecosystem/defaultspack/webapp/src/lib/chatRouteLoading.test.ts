import test from "node:test";
import assert from "node:assert/strict";

import {
  initialActiveWorkspaceTabIdForPathname,
  initialWorkspaceTabsForPathname,
  workspaceKindForPathname,
  workspaceUrlForKind,
} from "./workspaceRouting";
import { loadConversationForRefresh, resolveSupersededConversationRedirect } from "./chatRouteLoading";

test("refresh conversation loading honors URL chat id missing from the conversation list", async () => {
  const loaded: Array<string | null> = [];

  await loadConversationForRefresh({
    preferredId: null,
    activeConversationId: null,
    locationChatId: "old-chat",
    listedConversations: [{ id: "recent-chat" }],
    loadConversation: async (conversationId) => {
      loaded.push(conversationId);
    },
  });

  assert.deepEqual(loaded, ["old-chat"]);
});

test("refresh conversation loading falls back when direct URL chat load fails", async () => {
  const loaded: Array<string | null> = [];

  await loadConversationForRefresh({
    preferredId: null,
    activeConversationId: null,
    locationChatId: "missing-chat",
    listedConversations: [{ id: "recent-chat" }],
    loadConversation: async (conversationId) => {
      loaded.push(conversationId);
      if (conversationId === "missing-chat") {
        throw new Error("HTTP 404");
      }
    },
  });

  assert.deepEqual(loaded, ["missing-chat", "recent-chat"]);
});

test("refresh conversation loading prefers an explicit URL chat over existing active state", async () => {
  const loaded: Array<string | null> = [];

  await loadConversationForRefresh({
    preferredId: null,
    activeConversationId: "active-chat",
    locationChatId: "url-chat",
    listedConversations: [{ id: "active-chat" }, { id: "url-chat" }],
    loadConversation: async (conversationId) => {
      loaded.push(conversationId);
    },
  });

  assert.deepEqual(loaded, ["url-chat"]);
});

test("refresh conversation loading normalizes stale MiMo URL chats to the active chat", async () => {
  const attempts: Array<string | null> = [];
  const activated: Array<string | null> = [];
  let normalizedUrlChatId: string | null = null;
  const conversations: Record<string, { id: string; metadata?: Record<string, unknown> }> = {
    "stale-chat": {
      id: "stale-chat",
      metadata: {
        superseded: true,
        superseded_reason: "mimo_coding_company_inactive_chat",
        active_conversation_id: "active-chat",
        replacement_conversation_id: "active-chat",
      },
    },
    "active-chat": {
      id: "active-chat",
      metadata: {
        profile_id: "defaultspack.mimo_coding_company",
        company_id: "mimo-coding-company",
      },
    },
  };

  const appLikeLoadConversation = async (conversationId: string | null): Promise<void> => {
    attempts.push(conversationId);
    if (!conversationId) {
      activated.push(null);
      normalizedUrlChatId = null;
      return;
    }

    const conversation = conversations[conversationId];
    if (!conversation) {
      throw new Error(`Missing conversation ${conversationId}`);
    }

    const redirectedId = resolveSupersededConversationRedirect(conversation, conversationId);
    if (redirectedId) {
      await appLikeLoadConversation(redirectedId);
      return;
    }

    activated.push(conversationId);
    normalizedUrlChatId = conversationId;
  };

  await loadConversationForRefresh({
    preferredId: null,
    activeConversationId: null,
    locationChatId: "stale-chat",
    listedConversations: [{ id: "active-chat" }],
    loadConversation: appLikeLoadConversation,
  });

  assert.deepEqual(attempts, ["stale-chat", "active-chat"]);
  assert.deepEqual(activated, ["active-chat"]);
  assert.equal(normalizedUrlChatId, "active-chat");
});

test("superseded conversations redirect to active MiMo company conversation", () => {
  assert.equal(
    resolveSupersededConversationRedirect(
      {
        id: "stale-chat",
        metadata: {
          superseded: true,
          active_conversation_id: "live-chat",
        },
      },
      "stale-chat",
    ),
    "live-chat",
  );

  assert.equal(
    resolveSupersededConversationRedirect(
      {
        id: "live-chat",
        metadata: {
          superseded: true,
          active_conversation_id: "live-chat",
        },
      },
      "live-chat",
    ),
    null,
  );
});

test("workspace routing opens desktops route as the desktops workspace", () => {
  const tabs = initialWorkspaceTabsForPathname("/desktops", 1234);

  assert.equal(workspaceKindForPathname("/desktops"), "desktops");
  assert.equal(initialActiveWorkspaceTabIdForPathname("/desktops"), "workspace-tab-route-desktops");
  assert.deepEqual(tabs.map((tab) => tab.kind), ["chat", "desktops"]);
  assert.equal(tabs[1].id, "workspace-tab-route-desktops");
});

test("workspace routing opens calendar route as the calendar workspace", () => {
  const tabs = initialWorkspaceTabsForPathname("/calendar", 1234);

  assert.equal(workspaceKindForPathname("/calendar"), "calendar");
  assert.equal(initialActiveWorkspaceTabIdForPathname("/calendar"), "workspace-tab-route-calendar");
  assert.deepEqual(tabs.map((tab) => tab.kind), ["chat", "calendar"]);
  assert.equal(tabs[1].id, "workspace-tab-route-calendar");
});

test("workspace routing keeps desktops URL separate from chat conversations", () => {
  assert.equal(
    workspaceUrlForKind("desktops", "http://127.0.0.1:8766/p/profile-a/chat?chat=abc&pending=1#panel", "abc"),
    "/p/profile-a/desktops#panel",
  );
  assert.equal(
    workspaceUrlForKind("chat", "http://127.0.0.1:8766/p/profile-a/desktops#panel", "abc"),
    "/p/profile-a/chat?chat=abc#panel",
  );
});

test("workspace routing keeps calendar URL separate from stale chat conversations", () => {
  assert.equal(
    workspaceUrlForKind("calendar", "http://127.0.0.1:8766/p/profile-a/chat?chat=abc&pending=1#panel", "abc"),
    "/p/profile-a/calendar#panel",
  );
  assert.equal(
    workspaceUrlForKind("chat", "http://127.0.0.1:8766/p/profile-a/calendar#panel", "abc"),
    "/p/profile-a/chat?chat=abc#panel",
  );
});
