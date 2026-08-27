import test from "node:test";
import assert from "node:assert/strict";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import {
  DEFAULT_WORKSPACE_TAB_ID,
  WORKSPACE_TAB_CREATE_OPTIONS,
  WorkspaceTabBar,
  createWorkspaceTab,
  workspaceTabDisplayTitle,
  workspaceTabOption,
  workspaceTabsForConversation,
} from "./WorkspaceTabs";
import {
  buildConversationPresentations,
  conversationPresentation,
} from "../features/conversations/conversationPresentation";
import {
  initialActiveWorkspaceTabIdForPathname,
  initialWorkspaceTabsForPathname,
  workspaceKindForPathname,
  workspaceUrlForKind,
} from "../lib/workspaceRouting";

test("workspace tab options keep the extensible launch catalog", () => {
  assert.deepEqual(
    WORKSPACE_TAB_CREATE_OPTIONS.map((option) => option.kind),
    ["chat", "coding", "calendar", "kanban", "desktops", "subagents", "canvas", "tools", "browser"],
  );
  assert.equal(workspaceTabOption("browser").disabled, true);
  assert.equal(workspaceTabOption("subagents").label, "Subagents / Teams");
  assert.equal(workspaceTabOption("kanban").label, "Kanban");
  assert.equal(workspaceTabOption("desktops").label, "Desktops");
});

test("createWorkspaceTab uses option labels and supports deterministic overrides", () => {
  const tab = createWorkspaceTab("chat", { id: DEFAULT_WORKSPACE_TAB_ID, conversationId: "conv-1" }, 1_000);

  assert.deepEqual(tab, {
    id: DEFAULT_WORKSPACE_TAB_ID,
    kind: "chat",
    title: "AI Chat",
    conversationId: "conv-1",
    createdAt: 1_000,
  });
});

test("workspaceTabDisplayTitle falls back to the kind label", () => {
  assert.equal(workspaceTabDisplayTitle(createWorkspaceTab("tools", { title: "  " }, 1_000)), "Tools");
});

test("conversation presentation derives shared activity and read state", () => {
  const presentations = buildConversationPresentations([
    {
      id: "conversation-running",
      title: "Running chat",
      updated_at: 1_700_000_002_000,
      metadata: { icon_id: "terminal" },
    },
    {
      id: "conversation-waiting",
      title: "Waiting chat",
      updated_at: 1_700_000_001_000,
      metadata: { icon_id: "database" },
    },
  ], {
    activeConversationId: "conversation-running",
    runningConversationId: "conversation-running",
    pendingRequests: {
      "conversation-waiting": {
        status: "waiting for approval",
        updatedAt: 1_700_000_003_000,
      },
    },
    readAtByConversation: {
      "conversation-running": 1_700_000_001_000,
      "conversation-waiting": 1_700_000_002_000,
    },
  });

  assert.deepEqual(presentations["conversation-running"], {
    conversationId: "conversation-running",
    title: "Running chat",
    iconId: "terminal",
    activity: "running",
    unread: false,
    accessibleStatusLabel: "Running",
  });
  assert.equal(presentations["conversation-waiting"].activity, "waiting");
  assert.equal(presentations["conversation-waiting"].unread, true);
  assert.equal(
    presentations["conversation-waiting"].accessibleStatusLabel,
    "Waiting for approval or input, unread",
  );
});

test("workspace tabs render the canonical conversation title, safe icon, and attention", () => {
  const presentation = conversationPresentation({
    id: "conversation-1",
    title: "Renamed conversation",
    updated_at: 2_000,
    metadata: {
      icon_id: "database",
      icon_svg: '<svg onload="globalThis.pwned=true"></svg>',
      unread: true,
    },
  });
  const html = renderToStaticMarkup(createElement(WorkspaceTabBar, {
    tabs: [createWorkspaceTab("chat", {
      id: "tab-1",
      title: "Stale saved title",
      conversationId: "conversation-1",
    }, 1_000)],
    activeTabId: "other-tab",
    conversationPresentations: { "conversation-1": presentation },
    onSelect: () => undefined,
    onClose: () => undefined,
    onCreate: () => undefined,
  }));

  assert.match(html, /Renamed conversation/);
  assert.doesNotMatch(html, /Stale saved title/);
  assert.match(html, /data-conversation-icon-id="database"/);
  assert.match(html, /data-conversation-activity="done"/);
  assert.match(html, /data-conversation-unread="true"/);
  assert.doesNotMatch(html, /onload=/);
  assert.doesNotMatch(html, /globalThis\.pwned/);
});

test("completed conversations stop demanding attention after they are read", () => {
  const presentation = {
    conversationId: "conversation-1",
    title: "Read conversation",
    iconId: "chat",
    activity: "done" as const,
    unread: false,
    accessibleStatusLabel: null,
  };
  const html = renderToStaticMarkup(createElement(WorkspaceTabBar, {
    tabs: [createWorkspaceTab("chat", {
      id: "tab-1",
      conversationId: "conversation-1",
    }, 1_000)],
    activeTabId: "tab-1",
    conversationPresentations: { "conversation-1": presentation },
    onSelect: () => undefined,
    onClose: () => undefined,
    onCreate: () => undefined,
  }));

  assert.doesNotMatch(html, /data-conversation-activity/);
  assert.doesNotMatch(html, /role="status"/);
});

test("history activation reuses a matching tab and never duplicates the conversation", () => {
  const first = createWorkspaceTab("chat", {
    id: "tab-first",
    conversationId: "conversation-1",
  }, 1_000);
  const second = createWorkspaceTab("chat", {
    id: "tab-second",
    conversationId: "conversation-2",
  }, 2_000);

  const activation = workspaceTabsForConversation(
    [first, second],
    second.id,
    "conversation-1",
    3_000,
  );

  assert.equal(activation.activeTab.id, first.id);
  assert.equal(activation.tabs.length, 2);
  assert.equal(
    activation.tabs.filter((tab) => tab.conversationId === "conversation-1").length,
    1,
  );
});

test("history activation binds an empty chat before creating another tab", () => {
  const empty = createWorkspaceTab("chat", {
    id: "tab-empty",
    conversationId: null,
  }, 1_000);
  const activation = workspaceTabsForConversation(
    [empty],
    empty.id,
    "conversation-1",
    2_000,
  );

  assert.equal(activation.tabs.length, 1);
  assert.equal(activation.activeTab.conversationId, "conversation-1");
});


test("workspace routing preserves every enabled workspace kind", () => {
  for (const kind of ["calendar", "kanban", "desktops", "subagents", "canvas", "tools"] as const) {
    assert.equal(workspaceKindForPathname(`/${kind}`), kind);
    assert.equal(
      workspaceUrlForKind(kind, "https://example.test/p/profile-a/chat?chat=old#anchor"),
      `/p/profile-a/${kind}#anchor`,
    );
    assert.equal(initialWorkspaceTabsForPathname(`/${kind}`, 42).at(-1)?.kind, kind);
    assert.equal(initialActiveWorkspaceTabIdForPathname(`/${kind}`), `workspace-tab-route-${kind}`);
  }
});
