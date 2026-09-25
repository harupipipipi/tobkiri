import test from "node:test";
import assert from "node:assert/strict";
import { renderToStaticMarkup } from "react-dom/server";

import {
  DEFAULT_WORKSPACE_TAB_ID,
  NewTabDialog,
  WORKSPACE_TAB_CREATE_OPTIONS,
  WorkspaceTabBar,
  WorkspaceTabPanels,
  createWorkspaceTab,
  nextWorkspaceCreateOptionIndex,
  nextWorkspaceTabIndex,
  workspaceTabDisplayTitle,
  workspaceTabDomId,
  workspaceTabIdAfterClose,
  workspaceTabKeyboardAction,
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
  assert.equal(workspaceTabOption("browser").description, "近日公開");
  assert.equal(workspaceTabOption("subagents").label, "サブエージェント / チーム");
  assert.equal(workspaceTabOption("kanban").label, "カンバン");
  assert.equal(workspaceTabOption("desktops").label, "デスクトップ");
});

test("createWorkspaceTab uses option labels and supports deterministic overrides", () => {
  const tab = createWorkspaceTab("chat", { id: DEFAULT_WORKSPACE_TAB_ID, conversationId: "conv-1" }, 1_000);

  assert.deepEqual(tab, {
    id: DEFAULT_WORKSPACE_TAB_ID,
    kind: "chat",
    title: "AIチャット",
    conversationId: "conv-1",
    createdAt: 1_000,
  });
});

test("workspaceTabDisplayTitle falls back to the kind label", () => {
  assert.equal(workspaceTabDisplayTitle(createWorkspaceTab("tools", { title: "  " }, 1_000)), "ツール");
  assert.equal(workspaceTabDisplayTitle(createWorkspaceTab("chat", { title: "AI Chat" }, 1_000)), "AIチャット");
  assert.equal(workspaceTabDisplayTitle(createWorkspaceTab("chat", { title: "New Conversation" }, 1_000)), "新しい会話");
});

test("workspace tab keyboard model covers automatic activation and close keys", () => {
  assert.equal(workspaceTabKeyboardAction("ArrowLeft"), "ArrowLeft");
  assert.equal(workspaceTabKeyboardAction("ArrowRight"), "ArrowRight");
  assert.equal(workspaceTabKeyboardAction("Home"), "Home");
  assert.equal(workspaceTabKeyboardAction("End"), "End");
  assert.equal(workspaceTabKeyboardAction("Enter"), "activate");
  assert.equal(workspaceTabKeyboardAction(" "), "activate");
  assert.equal(workspaceTabKeyboardAction("Delete"), "close");
  assert.equal(workspaceTabKeyboardAction("Tab"), null);

  assert.equal(nextWorkspaceTabIndex(3, 0, "ArrowLeft"), 2);
  assert.equal(nextWorkspaceTabIndex(3, 2, "ArrowRight"), 0);
  assert.equal(nextWorkspaceTabIndex(3, 2, "Home"), 0);
  assert.equal(nextWorkspaceTabIndex(3, 0, "End"), 2);
});

test("new-tab dialog keyboard model contains focus across enabled choices", () => {
  assert.equal(nextWorkspaceCreateOptionIndex(8, 0, "ArrowLeft"), 7);
  assert.equal(nextWorkspaceCreateOptionIndex(8, 7, "ArrowRight"), 0);
  assert.equal(nextWorkspaceCreateOptionIndex(8, 3, "Home"), 0);
  assert.equal(nextWorkspaceCreateOptionIndex(8, 3, "End"), 7);
  assert.equal(nextWorkspaceCreateOptionIndex(8, 7, "Tab"), 0);
  assert.equal(nextWorkspaceCreateOptionIndex(8, 0, "Tab", true), 7);
  assert.equal(nextWorkspaceCreateOptionIndex(0, 0, "Tab"), null);
  assert.equal(nextWorkspaceCreateOptionIndex(8, 3, "Escape"), null);
});

test("closing the active tab selects the preceding neighbor predictably", () => {
  const tabs = [
    createWorkspaceTab("chat", { id: "chat" }, 1),
    createWorkspaceTab("coding", { id: "coding" }, 2),
    createWorkspaceTab("tools", { id: "tools" }, 3),
  ];
  assert.equal(workspaceTabIdAfterClose(tabs, "coding", "coding"), "chat");
  assert.equal(workspaceTabIdAfterClose(tabs, "chat", "chat"), "coding");
  assert.equal(workspaceTabIdAfterClose(tabs, "chat", "tools"), "chat");
  assert.equal(workspaceTabIdAfterClose([tabs[0]], "chat", "chat"), null);
});

test("workspace tabs expose stable screen-reader relationships and one roving stop", () => {
  const tabs = [
    createWorkspaceTab("chat", { id: "chat-home" }, 1),
    createWorkspaceTab("coding", { id: "coding-main" }, 2),
  ];
  const bar = renderToStaticMarkup(
    <WorkspaceTabBar
      tabs={tabs}
      activeTabId="coding-main"
      onSelect={() => undefined}
      onClose={() => undefined}
      onCreate={() => undefined}
    />,
  );
  const panels = renderToStaticMarkup(
    <WorkspaceTabPanels tabs={tabs} activeTabId="coding-main">
      <p>active content</p>
    </WorkspaceTabPanels>,
  );

  assert.match(bar, /role="tablist"/);
  assert.match(bar, /aria-label="開いているワークスペース"/);
  assert.match(bar, /aria-orientation="horizontal"/);
  assert.doesNotMatch(bar, /Open workspaces/);
  assert.match(bar, /overflow-x-auto/);
  assert.match(bar, new RegExp(`id="${workspaceTabDomId("coding-main", "tab")}"[^>]*aria-selected="true"[^>]*aria-controls="${workspaceTabDomId("coding-main", "panel")}"[^>]*tabindex="0"`));
  assert.match(bar, new RegExp(`id="${workspaceTabDomId("chat-home", "tab")}"[^>]*aria-selected="false"[^>]*aria-controls="${workspaceTabDomId("chat-home", "panel")}"[^>]*tabindex="-1"`));
  assert.match(panels, new RegExp(`id="${workspaceTabDomId("coding-main", "panel")}"[^>]*role="tabpanel"[^>]*aria-labelledby="${workspaceTabDomId("coding-main", "tab")}"`));
  assert.match(panels, new RegExp(`id="${workspaceTabDomId("chat-home", "panel")}"[^>]*role="tabpanel"[^>]*aria-labelledby="${workspaceTabDomId("chat-home", "tab")}"[^>]*hidden`));
});

test("one-tab state has no close control", () => {
  const tab = createWorkspaceTab("chat", { id: "only-tab" }, 1);
  const html = renderToStaticMarkup(
    <WorkspaceTabBar
      tabs={[tab]}
      activeTabId={tab.id}
      onSelect={() => undefined}
      onClose={() => undefined}
      onCreate={() => undefined}
    />,
  );
  assert.doesNotMatch(html, /を閉じる/);
  assert.match(html, /aria-label="新しいタブ"/);
});

test("new-tab dialog localizes choices and does not expose disabled items as actions", () => {
  const html = renderToStaticMarkup(
    <NewTabDialog
      dialogId="new-workspace"
      options={WORKSPACE_TAB_CREATE_OPTIONS}
      onCreate={() => undefined}
      onDismiss={() => undefined}
    />,
  );
  assert.match(html, /role="dialog"/);
  assert.match(html, /aria-modal="true"/);
  assert.match(html, /新しいワークスペース/);
  assert.match(html, /data-workspace-disabled-option="browser"/);
  assert.match(html, /role="note" aria-disabled="true"/);
  assert.doesNotMatch(html, /data-workspace-create-option="browser"/);
  assert.equal((html.match(/data-workspace-create-option=/g) ?? []).length, 8);
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
