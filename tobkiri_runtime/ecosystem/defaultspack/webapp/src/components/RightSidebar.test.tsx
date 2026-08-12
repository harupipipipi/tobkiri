import test from "node:test";
import assert from "node:assert/strict";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import {
  getRailFloatingMenuPosition,
  RightSidebar,
  shouldShowToolManagerEmptyState,
  sidebarActionDisabledReason,
  toolManagerBaseItemsForNameSearch,
} from "./RightSidebar";
import { PromptSidebarWidget } from "./prompts/PromptSidebarWidget";

const noop = () => undefined;

function buttonWithAccessibleName(html: string, name: string) {
  const escapedName = name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const openingTag = ["<", "button"].join("");
  return new RegExp(`${openingTag}(?=[^>]*aria-label="${escapedName}")(?=[^>]*type="button")[^>]*>([\\s\\S]*?)<\\/button>`).exec(html);
}

test("share and export actions are disabled until a conversation is saved", () => {
  assert.equal(
    sidebarActionDisabledReason({ id: "conversation.export", label: "Export Active Conversation" }, null),
    "エクスポートする会話がありません。会話を保存してから実行してください。",
  );
  assert.equal(
    sidebarActionDisabledReason({ id: "conversation.share", label: "Create Local Share Link" }, ""),
    "共有する会話がありません。会話を保存してから実行してください。",
  );
  assert.equal(
    sidebarActionDisabledReason({ id: "conversation.export", label: "Export Active Conversation" }, "chat-1"),
    "",
  );
  assert.equal(
    sidebarActionDisabledReason({ id: "artifacts.list", label: "List Artifacts" }, null),
    "",
  );
});

test("left sidebar default does not render every tool detail panel", () => {
  const html = renderToStaticMarkup(
    createElement(RightSidebar, {
      items: [
        { id: "vision_tool", label: "Vision Tool", category: "tool", description: "Detail panel text" },
      ],
      settingsValues: {
        sidebar: { pinned_item_ids: [], starred_item_ids: [], custom_tool_tags: {}, ui_placements: [] },
        tools: { disabled_tool_ids: [], hidden_tool_ids: [] },
      },
      settingsSections: [],
      selectedToolIds: [],
      onSettingChange: noop,
      onOpenSettings: noop,
    }),
  );

  assert.doesNotMatch(html, /Detail panel text/);
});

test("risky tool detail keeps a prominent needs approval affordance", () => {
  const html = renderToStaticMarkup(
    createElement(RightSidebar, {
      activeItemId: "node_exec",
      items: [
        {
          id: "node_exec",
          label: "Node Exec",
          category: "tool",
          description: "Execute sandboxed Node code.",
          risk: "high",
          tags: ["sandbox", "agent_os", "artifact_workspace"],
          tool_info: {
            requires_approval: true,
            approval_policy: "runtime",
          },
          panel: {
            kind: "tool",
            notes: ["Requires approval."],
          },
        },
      ],
      settingsValues: {
        sidebar: { pinned_item_ids: [], starred_item_ids: [], custom_tool_tags: {}, ui_placements: [] },
        tools: { disabled_tool_ids: [], hidden_tool_ids: [] },
      },
      settingsSections: [],
      selectedToolIds: [],
      onSettingChange: noop,
      onOpenSettings: noop,
    }),
  );

  assert.match(html, /data-testid="tool-detail-needs-approval"/);
  assert.match(html, />Needs approval</);
  assert.match(html, /Approval policy: runtime/);
  assert.match(html, />danger</);
  assert.match(html, />risk:high</);
});

test("right sidebar initially focuses the rail on activities", () => {
  const html = renderToStaticMarkup(
    createElement(RightSidebar, {
      items: [
        { id: "browser", label: "Browser", category: "activity" },
        { id: "tool_a", label: "Tool A", category: "tool" },
        { id: "widget_a", label: "Widget A", category: "widget" },
      ],
      settingsValues: {
        sidebar: { pinned_item_ids: [], starred_item_ids: [], custom_tool_tags: {}, ui_placements: [] },
        tools: { disabled_tool_ids: [], hidden_tool_ids: [] },
      },
      settingsSections: [],
      selectedToolIds: [],
      onSettingChange: noop,
      onOpenSettings: noop,
    }),
  );

  assert.match(html, /title="Filter: Activities"/);
  assert.match(html, /title="Browser"/);
  assert.doesNotMatch(html, /title="other \(1\)"/);
  assert.doesNotMatch(html, /title="Widget A"/);
});

test("right sidebar rail avoids transform and replayed entrance animations", () => {
  const html = renderToStaticMarkup(
    createElement(RightSidebar, {
      items: [
        { id: "browser", label: "Browser", category: "activity" },
        { id: "browser_companion", label: "Browser Companion", category: "tool" },
      ],
      settingsValues: {
        sidebar: { pinned_item_ids: [], starred_item_ids: [], custom_tool_tags: {}, ui_placements: [] },
        tools: { disabled_tool_ids: [], hidden_tool_ids: [] },
      },
      settingsSections: [],
      selectedToolIds: ["browser_companion"],
      onSettingChange: noop,
      onOpenSettings: noop,
    }),
  );

  assert.match(html, /title="Browser"/);
  assert.doesNotMatch(html, /title="Browser Companion"/);
  assert.doesNotMatch(html, /hover:scale/);
  assert.doesNotMatch(html, /active:scale/);
  assert.doesNotMatch(html, /rumi-stagger-tight/);
  assert.doesNotMatch(html, /transition-\[background-color,color,box-shadow\]/);
});

test("right sidebar starred tools rail button keeps a descriptive accessible name", () => {
  const html = renderToStaticMarkup(
    createElement(RightSidebar, {
      items: [
        { id: "browser_companion", label: "Browser Companion", category: "tool" },
      ],
      settingsValues: {
        sidebar: { pinned_item_ids: [], starred_item_ids: [], custom_tool_tags: {}, ui_placements: [] },
        tools: { disabled_tool_ids: [], hidden_tool_ids: [] },
      },
      settingsSections: [],
      selectedToolIds: [],
      onSettingChange: noop,
      onOpenSettings: noop,
    }),
  );

  const starredButton = buttonWithAccessibleName(html, "Starred tools");
  assert.ok(starredButton, "Starred tools rail control should remain targetable by native button role and accessible name");
  assert.doesNotMatch(starredButton[1], />\s*1\s*</);
});

test("right sidebar starred tools rail button keeps its name when count is nonzero", () => {
  const html = renderToStaticMarkup(
    createElement(RightSidebar, {
      items: [
        { id: "browser_companion", label: "Browser Companion", category: "tool" },
      ],
      settingsValues: {
        sidebar: {
          pinned_item_ids: [],
          starred_item_ids: ["browser_companion"],
          custom_tool_tags: {},
          ui_placements: [],
        },
        tools: { disabled_tool_ids: [], hidden_tool_ids: [] },
      },
      settingsSections: [],
      selectedToolIds: [],
      onSettingChange: noop,
      onOpenSettings: noop,
    }),
  );

  const starredButton = buttonWithAccessibleName(html, "Starred tools (1)");
  assert.ok(starredButton, "Starred tools rail control should remain targetable by native button role and count-aware accessible name");
  assert.match(starredButton[1], />1<\/span>/);
  assert.equal(buttonWithAccessibleName(html, "1"), null);
});

test("right sidebar does not auto-open employees on initial render", () => {
  const html = renderToStaticMarkup(
    createElement(RightSidebar, {
      items: [],
      settingsValues: {
        sidebar: { pinned_item_ids: [], starred_item_ids: [], custom_tool_tags: {}, ui_placements: [] },
        tools: { disabled_tool_ids: [], hidden_tool_ids: [] },
      },
      settingsSections: [],
      selectedToolIds: [],
      companyPanel: createElement("div", null, "Employee workspace content"),
      onSettingChange: noop,
      onOpenSettings: noop,
    }),
  );

  assert.match(html, /title="Employees"/);
  assert.doesNotMatch(html, /Employee workspace content/);
});

test("advanced usage commands can open context token details", () => {
  const html = renderToStaticMarkup(
    createElement(RightSidebar, {
      activeItemId: "__context_usage__:1",
      items: [],
      settingsValues: {
        sidebar: { pinned_item_ids: [], starred_item_ids: [], custom_tool_tags: {}, ui_placements: [] },
        tools: { disabled_tool_ids: [], hidden_tool_ids: [] },
      },
      settingsSections: [],
      selectedToolIds: [],
      contextUsage: { usedTokens: 1250, maxContext: 8000, ratio: 0.15625, label: "16%" },
      onSettingChange: noop,
      onOpenSettings: noop,
    }),
  );

  assert.match(html, /data-testid="context-usage-panel"/);
  assert.match(html, />1250</);
  assert.match(html, />8000</);
  assert.match(html, />16%</);
});

test("right sidebar keeps raw tool groups off the initial activity rail", () => {
  const html = renderToStaticMarkup(
    createElement(RightSidebar, {
      items: Array.from({ length: 12 }, (_value, index) => ({
        id: `tool_${index}`,
        label: `Tool ${index}`,
        category: "tool" as const,
        ui: { group_id: `group-${String(index).padStart(2, "0")}`, group_label: `Group ${index}` },
      })),
      settingsValues: {
        sidebar: { pinned_item_ids: [], starred_item_ids: [], custom_tool_tags: {}, ui_placements: [] },
        tools: { disabled_tool_ids: [], hidden_tool_ids: [] },
      },
      settingsSections: [],
      selectedToolIds: [],
      onSettingChange: noop,
      onOpenSettings: noop,
    }),
  );

  assert.doesNotMatch(html, /title="その他の機能 \(4 groups\)"/);
  assert.doesNotMatch(html, /title="Group 11 \(1\)"/);
});

test("tool manager name search no-match does not keep stale tool cards", () => {
  const allItems = [
    { id: "browser_companion", label: "Browser Companion", category: "tool" as const },
    { id: "terminal", label: "Terminal", category: "tool" as const },
    { id: "prompt_usage", label: "Prompt Usage", category: "widget" as const },
  ];

  const visibleTools = toolManagerBaseItemsForNameSearch(allItems, [], "zzzz-no-tool-qa");

  assert.equal(visibleTools.length, 0);
  assert.equal(visibleTools.some((item) => item.label === "Browser Companion"), false);
});

test("tool manager name search no-match shows the empty state", () => {
  assert.equal(shouldShowToolManagerEmptyState({
    toolCount: 0,
    sidebarSearchQuery: "zzzz-no-tool-qa",
    toolManagerSearchQuery: "",
    activeTagFilter: null,
    showStarredOnly: false,
  }), true);
  assert.equal(shouldShowToolManagerEmptyState({
    toolCount: 1,
    sidebarSearchQuery: "browser",
    toolManagerSearchQuery: "",
    activeTagFilter: null,
    showStarredOnly: false,
  }), false);
  assert.equal(shouldShowToolManagerEmptyState({
    toolCount: 0,
    sidebarSearchQuery: "",
    toolManagerSearchQuery: "",
    activeTagFilter: null,
    showStarredOnly: false,
  }), false);
});

test("YOLO switch and Model Manager can be pinned", () => {
  const html = renderToStaticMarkup(
    createElement(RightSidebar, {
      items: [],
      settingsValues: {
        sidebar: {
          pinned_item_ids: [],
          starred_item_ids: [],
          custom_tool_tags: {},
          ui_placements: [
            { id: "yolo-switch", surface: "right_sidebar" },
            { id: "model-manager", surface: "right_sidebar" },
          ],
        },
        tools: { disabled_tool_ids: [], hidden_tool_ids: [] },
      },
      settingsSections: [{ id: "models", label: "Models", fields: [] }],
      selectedToolIds: [],
      yoloMode: true,
      onSettingChange: noop,
      onOpenSettings: noop,
      onToggleYolo: noop,
      onOpenSettingsSection: noop,
    }),
  );

  assert.match(html, /title="YOLO Switch"/);
  assert.match(html, /title="Model Manager"/);
});

test("right sidebar exposes workspace tabs as a vertical switcher widget", () => {
  const html = renderToStaticMarkup(
    createElement(RightSidebar, {
      items: [],
      settingsValues: {
        sidebar: { pinned_item_ids: [], starred_item_ids: [], custom_tool_tags: {}, ui_placements: [] },
        tools: { disabled_tool_ids: [], hidden_tool_ids: [] },
      },
      settingsSections: [],
      selectedToolIds: [],
      workspaceTabs: [
        { id: "tab-chat", kind: "chat", title: "Planning", conversationId: "conv-1", createdAt: 1 },
        { id: "tab-calendar", kind: "calendar", title: "Calendar", createdAt: 2 },
      ],
      activeWorkspaceTabId: "tab-chat",
      onSettingChange: noop,
      onOpenSettings: noop,
      onWorkspaceTabSelect: noop,
      onWorkspaceTabClose: noop,
      onWorkspaceTabCreate: noop,
    }),
  );

  assert.match(html, /title="Workspace tabs"/);
  assert.match(html, /aria-label="Workspace tabs"/);
  assert.match(html, />2</);
});

test("right sidebar exposes current prompts as a rail widget", () => {
  const html = renderToStaticMarkup(
    createElement(RightSidebar, {
      items: [],
      settingsValues: {
        sidebar: { pinned_item_ids: [], starred_item_ids: [], custom_tool_tags: {}, ui_placements: [] },
        tools: { disabled_tool_ids: [], hidden_tool_ids: [] },
      },
      settingsSections: [],
      selectedToolIds: [],
      promptUsage: {
        active_count: 2,
        token_estimate: { total: 155 },
        segments: [
          { id: "default_chat", prompt_id: "default_chat", label: "default_chat", status: "active", tokens: 124 },
          { id: "calculator", prompt_id: "calculator", label: "calculator", status: "active", tokens: 31 },
        ],
      },
      onLoadPromptActive: async () => ({ segments: [] }),
      onTogglePromptEdge: async () => ({ segments: [] }),
      onSettingChange: noop,
      onOpenSettings: noop,
    }),
  );

  assert.match(html, /title="Current prompts"/);
  assert.match(html, /aria-label="Current prompts"/);
  assert.match(html, />2</);
});

test("prompt sidebar widget lists prompt name and token count before details", () => {
  const html = renderToStaticMarkup(
    createElement(PromptSidebarWidget, {
      profileId: "default-profile",
      conversationId: "conversation-1",
      initialUsage: {
        active_count: 1,
        token_estimate: { total: 124 },
        segments: [
          {
            id: "default_chat",
            prompt_id: "default_chat",
            label: "default_chat",
            kind: "pack",
            status: "active",
            tokens: 124,
            reason: "Selected by the active profile.",
          },
        ],
      },
      loadPromptActive: async () => ({ segments: [] }),
      togglePromptEdge: async () => ({ segments: [] }),
    }),
  );

  assert.match(html, /現在のプロンプト/);
  assert.match(html, /default_chat/);
  assert.match(html, /124/);
  assert.doesNotMatch(html, /Selected by the active profile/);
});

test("prompt sidebar widget exposes chat prompt disclosure toggle", () => {
  const html = renderToStaticMarkup(
    createElement(PromptSidebarWidget, {
      profileId: "default-profile",
      initialUsage: {
        active_count: 0,
        token_estimate: { total: 0 },
        segments: [],
      },
      loadPromptActive: async () => ({ segments: [] }),
      togglePromptEdge: async () => ({ segments: [] }),
      showChatPromptUsage: false,
      onToggleChatPromptUsage: noop,
    }),
  );

  assert.match(html, /チャット内の Prompt used/);
  assert.match(html, /メッセージ下では非表示/);
  assert.match(html, /aria-pressed="false"/);
  assert.match(html, />Off</);
});

test("right sidebar floating menus clamp to the viewport", () => {
  assert.deepEqual(
    getRailFloatingMenuPosition(
      { left: 1238, top: 627 },
      { width: 224, height: 360, viewportWidth: 1280, viewportHeight: 720 },
    ),
    { top: 352, right: 50 },
  );

  assert.deepEqual(
    getRailFloatingMenuPosition(
      { left: 8, top: -40 },
      { width: 224, height: 360, viewportWidth: 320, viewportHeight: 480 },
    ),
    { top: 8, right: 88 },
  );
});
