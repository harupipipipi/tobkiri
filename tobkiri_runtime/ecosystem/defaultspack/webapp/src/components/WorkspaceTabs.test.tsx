import test from "node:test";
import assert from "node:assert/strict";

import {
  DEFAULT_WORKSPACE_TAB_ID,
  WORKSPACE_TAB_CREATE_OPTIONS,
  createWorkspaceTab,
  workspaceTabDisplayTitle,
  workspaceTabOption,
} from "./WorkspaceTabs";
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


test("workspace routing preserves every enabled workspace kind", () => {
  for (const kind of ["calendar", "kanban", "desktops", "subagents", "canvas", "tools"] as const) {
    assert.equal(workspaceKindForPathname(`/${kind}`), kind);
    assert.equal(workspaceUrlForKind(kind, "https://example.test/chat?chat=old#anchor"), `/${kind}#anchor`);
    assert.equal(initialWorkspaceTabsForPathname(`/${kind}`, 42).at(-1)?.kind, kind);
    assert.equal(initialActiveWorkspaceTabIdForPathname(`/${kind}`), `workspace-tab-route-${kind}`);
  }
});
