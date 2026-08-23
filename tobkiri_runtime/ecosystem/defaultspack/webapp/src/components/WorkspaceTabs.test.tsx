import test from "node:test";
import assert from "node:assert/strict";

import {
  DEFAULT_WORKSPACE_TAB_ID,
  WORKSPACE_TAB_CREATE_OPTIONS,
  closeWorkspaceTab,
  createWorkspaceTab,
  restoreLastClosedWorkspaceTab,
  workspaceTabDisplayTitle,
  workspaceTabOption,
} from "./WorkspaceTabs";
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
