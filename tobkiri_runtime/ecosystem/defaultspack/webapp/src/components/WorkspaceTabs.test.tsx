import test from "node:test";
import assert from "node:assert/strict";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import {
  DEFAULT_WORKSPACE_TAB_ID,
  WORKSPACE_TAB_CREATE_OPTIONS,
  closeWorkspaceTab,
  CLOSED_WORKSPACE_TAB_LIMIT,
  rememberClosedWorkspaceTab,
  WorkspaceTabBar,
  createWorkspaceTab,
  restoreLastClosedWorkspaceTab,
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
import { workspaceTabShortcutAction } from "../lib/keyboardShortcuts";

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

test("workspace tab shortcuts support Ctrl and Cmd in text inputs without breaking IME or repeats", () => {
  const input = { tagName: "INPUT", type: "text", getAttribute: () => null } as unknown as EventTarget;
  assert.equal(workspaceTabShortcutAction({ ctrlKey: true, key: "t", target: input }), "create_chat");
  assert.equal(workspaceTabShortcutAction({ metaKey: true, key: "w", target: input }), "close_active");
  assert.equal(workspaceTabShortcutAction({ ctrlKey: true, shiftKey: true, key: "T", target: input }), "restore_last_closed");
  assert.equal(workspaceTabShortcutAction({ ctrlKey: true, key: "t", isComposing: true }), null);
  assert.equal(workspaceTabShortcutAction({ ctrlKey: true, key: "t", repeat: true }), null);
  assert.equal(workspaceTabShortcutAction({ ctrlKey: true, key: "t", defaultPrevented: true }), null);
  assert.equal(workspaceTabShortcutAction({ ctrlKey: true, altKey: true, key: "t" }), null);
});

test("workspace tab close and restore preserve state, adjacency, and LIFO order", () => {
  const chat = createWorkspaceTab("chat", { id: "chat", conversationId: "conv-1", title: "Chat" }, 1);
  const kanban = createWorkspaceTab("kanban", {
    id: "kanban",
    kanbanScope: { type: "company", id: "team-1" },
    kanbanScopeLabel: "Team One",
    title: "Board",
  }, 2);
  const tools = createWorkspaceTab("tools", { id: "tools", title: "Tools" }, 3);

  const firstClose = closeWorkspaceTab([chat, kanban, tools], "kanban", "kanban");
  assert.deepEqual(firstClose.tabs.map((tab) => tab.id), ["chat", "tools"]);
  assert.equal(firstClose.nextActiveTab?.id, "chat");
  assert.deepEqual(firstClose.closedTab?.tab.kanbanScope, { type: "company", id: "team-1" });

  const secondClose = closeWorkspaceTab(firstClose.tabs, "tools", "tools");
  assert.equal(secondClose.nextActiveTab?.id, "chat");
  const stack = [firstClose.closedTab!, secondClose.closedTab!];
  const restoredTools = restoreLastClosedWorkspaceTab(secondClose.tabs, stack);
  assert.equal(restoredTools.restoredTab?.id, "tools");
  assert.deepEqual(restoredTools.tabs.map((tab) => tab.id), ["chat", "tools"]);
  const restoredKanban = restoreLastClosedWorkspaceTab(restoredTools.tabs, restoredTools.closedTabs);
  assert.equal(restoredKanban.restoredTab?.id, "kanban");
  assert.deepEqual(restoredKanban.tabs.map((tab) => tab.id), ["chat", "kanban", "tools"]);
  assert.equal(restoredKanban.restoredTab?.kanbanScopeLabel, "Team One");
});

test("workspace tab close and restore are no-ops when they cannot be handled", () => {
  const only = createWorkspaceTab("chat", { id: "only" }, 1);
  const close = closeWorkspaceTab([only], "only", "only");
  assert.equal(close.closedTab, null);
  assert.equal(close.tabs[0], only);
  const restore = restoreLastClosedWorkspaceTab([only], []);
  assert.equal(restore.restoredTab, null);
  assert.equal(restore.tabs[0], only);
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
  assert.match(html, /data-conversation-activity="idle"/);
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


test("closed workspace history keeps only the newest bounded entries", () => {
  let history: Parameters<typeof rememberClosedWorkspaceTab>[0] = [];
  for (let index = 0; index < CLOSED_WORKSPACE_TAB_LIMIT + 9; index += 1) {
    history = rememberClosedWorkspaceTab(history, {
      index: 0,
      tab: createWorkspaceTab("chat", { id: `closed-${index}` }, index),
    });
  }
  assert.equal(history.length, CLOSED_WORKSPACE_TAB_LIMIT);
  assert.equal(history[0].tab.id, "closed-9");
  assert.equal(restoreLastClosedWorkspaceTab([], history).restoredTab?.id, `closed-${CLOSED_WORKSPACE_TAB_LIMIT + 8}`);
});

test("cancelled conversations and unread idle updates are never reported as failures or success", () => {
  const cancelled = buildConversationPresentations([{ id: "cancelled", title: "Cancel", updated_at: 0, metadata: { status: "cancelled" } }]).cancelled;
  assert.equal(cancelled.activity, "cancelled");
  assert.equal(cancelled.accessibleStatusLabel, "Cancelled");
  const idle = buildConversationPresentations([{ id: "idle", title: "Idle", updated_at: 0, metadata: { unread: true } }]).idle;
  assert.equal(idle.activity, "idle");
  assert.equal(idle.accessibleStatusLabel, "Unread update");
});

test("conversation icon dictionaries reject inherited keys without rendering arbitrary markup", () => {
  for (const iconId of ["constructor", "__proto__", "toString", "hasOwnProperty"]) {
    const presentations = buildConversationPresentations([{ id: "safe", title: "Safe", updated_at: 0, metadata: { icon_id: iconId } }]);
    const html = renderToStaticMarkup(createElement(WorkspaceTabBar, {
      tabs: [createWorkspaceTab("chat", { conversationId: "safe" })],
      activeTabId: "none", conversationPresentations: presentations,
      onSelect: () => undefined, onClose: () => undefined, onCreate: () => undefined,
    }));
    assert.match(html, /lucide-message-square/);
  }
});


test("extension tab close and restore preserve captured catalog identity without rebinding", () => {
  const reference = {
    contributionId: "schedule.view", ownerPackId: "schedule", descriptorHash: "descriptor",
    profileId: "one", profileRevision: "revision", activationId: "activation",
    planHash: "plan", catalogHash: "catalog",
  };
  const home = createWorkspaceTab("chat", { id: "home" });
  const extension = createWorkspaceTab("extension", { id: "extension", title: "Schedules", viewReference: reference });
  reference.profileId = "mutated";
  assert.equal(extension.viewReference?.profileId, "one");
  const closed = closeWorkspaceTab([home, extension], extension.id, extension.id);
  const restored = restoreLastClosedWorkspaceTab(closed.tabs, [closed.closedTab!]);
  assert.deepEqual(restored.restoredTab?.viewReference, extension.viewReference);
  assert.equal(restored.restoredTab?.title, "Schedules");
  assert.equal(workspaceTabOption("extension").label, "Pack view");
  assert.equal(WORKSPACE_TAB_CREATE_OPTIONS.some((option) => option.kind === "extension"), false);
});
