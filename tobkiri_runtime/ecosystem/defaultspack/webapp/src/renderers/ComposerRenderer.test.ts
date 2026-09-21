import test from "node:test";
import assert from "node:assert/strict";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { CodingWorkspacePicker } from "../components/coding/CodingWorkspacePicker";
import {
  atMentionMenuKeyAction,
  AtMentionMenu,
  filterAtMentionFiles,
  insertAtMentionText,
  composerChromeWidgetStyle,
  composerHelperCopy,
  composerModelControlWidth,
  composerPlaceholderCopy,
  modelDropdownPlacementClassName,
  modelCandidateMenuKeyAction,
  modelCandidatePopupStyleForAnchor,
  nextModelCandidateIndex,
  profileNeedsApiKey,
  ComposerRenderer,
  composerToolMentionWidget,
  filterComposerToolMentions,
  filterModelProfilesBySearch,
  resolveComposerWidgetDrop,
  shouldFocusComposerForSlashKey,
  toolMentionIdsFromText,
} from "./ComposerRenderer";
import { COMPOSER_BUTTON_DROP, COMPOSER_PANEL_DROP, COMPOSER_SELECTOR_DROP, COMPOSER_TOGGLE_DROP } from "../lib/toolUi";
import type { ComposerCommandItem } from "../lib/api";

test("composer file mention filters string context files", () => {
  const files = ["README.md", "src/App.tsx", "docs/context.md"];

  assert.deepEqual(filterAtMentionFiles(files, "md"), ["README.md", "docs/context.md"]);
  assert.equal(typeof filterAtMentionFiles(files, "")[0], "string");
});

test("composer file mention insertion keeps @ text for workspace attachment flow", () => {
  const result = insertAtMentionText("please @REA now", 11, "README.md");

  assert.deepEqual(result, {
    value: "please @README.md  now",
    cursor: 18,
  });
});

test("composer known dotted mention keeps Japanese-adjacent cursor parity", () => {
  const result = insertAtMentionText(
    "確認@README.md",
    "確認@README.md".length,
    "README.md",
    ["README.md"],
  );

  assert.deepEqual(result, {
    value: "確認@README.md ",
    cursor: "確認@README.md ".length,
  });
});

test("composer tool mentions resolve searchable tools and JSON metadata", () => {
  const tools = [
    {
      id: "web_search",
      label: "Web Search",
      category: "tool",
      description: "Search the web.",
      tags: ["research"],
      ui: { composer_label: "Web Search" },
    },
    {
      id: "coding_file_read",
      label: "Read File",
      category: "tool",
      description: "Read a workspace file.",
      tags: ["coding", "file"],
    },
  ];

  assert.deepEqual(filterComposerToolMentions(tools, "workspace").map((tool) => tool.id), ["coding_file_read"]);
  assert.deepEqual(filterComposerToolMentions(tools, "web").map((tool) => tool.id), ["web_search"]);
  assert.deepEqual(toolMentionIdsFromText("Use @web_search then @Read_File.", tools), ["web_search", "coding_file_read"]);
  assert.deepEqual(
    toolMentionIdsFromText(
      "お願い@mcp.server",
      [...tools, { id: "mcp.server", label: "MCP Server" }],
    ),
    ["mcp.server"],
  );
  assert.deepEqual(
    toolMentionIdsFromText(
      "ユーザー@example.com",
      [...tools, { id: "mcp.server", label: "MCP Server" }],
    ),
    [],
  );
  assert.deepEqual(composerToolMentionWidget(tools[0]), {
    id: "web_search",
    type: "tool",
    label: "Web Search",
    enabled: true,
    widgetKind: "tool_toggle",
    action: undefined,
    sourceItemId: "web_search",
    description: "Search the web.",
    icon: undefined,
    metadata: {
      source: "composer_at_mention",
      mention: {
        id: "web_search",
        kind: "tool",
        label: "Web Search",
        syntax: "@Web Search",
        tool_id: "web_search",
      },
      tool: {
        id: "web_search",
        label: "Web Search",
        category: "tool",
        description: "Search the web.",
        tags: ["research"],
        ui: { composer_label: "Web Search" },
      },
    },
  });
});

test("composer mention filters retain known tools and files while typing", () => {
  const tools = [
    {
      id: "web_search",
      label: "Web Search",
      category: "tool",
      description: "Search the web.",
      tags: ["research"],
      ui: { composer_label: "Web Search" },
    },
    {
      id: "calculator",
      label: "Calculator",
      category: "tool",
      description: "Compute arithmetic.",
      tags: ["math"],
    },
    {
      id: "browser_computer",
      label: "Browser Computer",
      category: "tool",
      description: "Control the browser and computer.",
      tags: ["browser", "computer"],
    },
  ];

  assert.deepEqual(filterComposerToolMentions(tools, "web").map((tool) => tool.id), ["web_search"]);
  assert.deepEqual(filterComposerToolMentions(tools, "calculator").map((tool) => tool.id), ["calculator"]);
  assert.deepEqual(filterComposerToolMentions(tools, "browser").map((tool) => tool.id), ["browser_computer"]);
  assert.deepEqual(filterComposerToolMentions([{ ...tools[0], disabled: true }], "web"), []);
  assert.deepEqual(filterAtMentionFiles(["src/App.tsx", "README.md"], "README"), ["README.md"]);
});

test("structured composer controls stay above the textarea without rewriting its text", () => {
  const html = renderToStaticMarkup(
    createElement(ComposerRenderer, {
      input: "本文だけを保持",
      placeholder: "メッセージを入力...",
      isGenerating: false,
      selectedProfile: {
        profile_id: "stub/default",
        display_name: "Stub Default",
        provider_id: "stub",
        model_id: "default",
      },
      favoriteProfiles: [],
      inlineExtensions: [],
      belowExtensions: [],
      thinkingLevel: null,
      contextUsage: { ratio: 0, usedTokens: 0, maxContext: 0, label: "0%" },
      composerInput: {
        id: "structured",
        label: "入力オプション",
        fields: [
          { id: "intent", type: "select", options: [{ value: "review", label: "レビュー" }] },
          { id: "constraints", type: "text" },
        ],
      },
      structuredInputValues: { intent: "review" },
      onInputChange: () => undefined,
      onStructuredInputChange: () => undefined,
      onSubmit: () => undefined,
      onModelProfileSelect: () => undefined,
      onThinkingLevelChange: () => undefined,
    }),
  );

  assert.match(html, /data-structured-composer="structured"/);
  assert.match(html, /本文だけを保持/);
  assert.doesNotMatch(html, /&lt;rumi:input&gt;/);
});

test("composer mention Enter selects candidates and does not submit raw unmatched text", () => {
  assert.deepEqual(atMentionMenuKeyAction("Enter", false, 0, 2), {
    handled: true,
    type: "select",
    index: 0,
  });
  assert.deepEqual(atMentionMenuKeyAction("Tab", false, 9, 2), {
    handled: true,
    type: "select",
    index: 1,
  });
  assert.deepEqual(atMentionMenuKeyAction("Enter", false, 0, 0), {
    handled: false,
  });
  assert.deepEqual(atMentionMenuKeyAction("Tab", false, 0, 0), {
    handled: false,
  });
  assert.deepEqual(atMentionMenuKeyAction("Escape", false, 0, 0), {
    handled: true,
    type: "close",
  });
  assert.deepEqual(insertAtMentionText("Use @web", 8, "Web Search"), {
    value: "Use @Web Search ",
    cursor: 16,
  });
  assert.deepEqual(atMentionMenuKeyAction("Enter", true, 0, 2), { handled: false });
  assert.deepEqual(atMentionMenuKeyAction("Tab", true, 0, 2), { handled: false });
});

test("empty mention listbox is visible and announced", () => {
  const html = renderToStaticMarkup(createElement(AtMentionMenu, {
    candidates: [],
    activeIndex: 0,
    onActiveIndexChange: () => undefined,
    onSelect: () => undefined,
    onClose: () => undefined,
  }));

  assert.match(html, /role="listbox"/);
  assert.match(html, /role="status"/);
  assert.match(html, /aria-live="polite"/);
  assert.match(html, /一致する候補はありません/);
  assert.doesNotMatch(html, /role="option"/);
});

test("selected references render inline while explicit drops keep the widget row", () => {
  const baseProps = {
    input: "Use @web_search then review",
    placeholder: "Message Rumi...",
    isGenerating: false,
    selectedProfile: {
      profile_id: "stub/default",
      display_name: "Stub Default",
      provider_id: "stub",
      model_id: "default",
    },
    favoriteProfiles: [],
    inlineExtensions: [{ id: "web_search", label: "Web Search", category: "tool" }],
    belowExtensions: [],
    thinkingLevel: null,
    contextUsage: { ratio: 0, usedTokens: 0, maxContext: 0, label: "0%" },
    onInputChange: () => undefined,
    onSubmit: () => undefined,
    onModelProfileSelect: () => undefined,
    onThinkingLevelChange: () => undefined,
  };
  const referenceHtml = renderToStaticMarkup(createElement(ComposerRenderer, {
    ...baseProps,
    entityReferences: [{ kind: "tool", id: "web_search", syntax: "@web_search" }],
  }));

  assert.match(referenceHtml, /data-composer-inline-reference="tool:web_search"/);
  assert.match(referenceHtml, />Web Search</);
  assert.match(referenceHtml, /text-sky-200/);
  assert.match(referenceHtml, /text-transparent caret-transparent/);
  assert.doesNotMatch(referenceHtml, /metadata=|composer_at_mention/);

  const droppedHtml = renderToStaticMarkup(createElement(ComposerRenderer, {
    ...baseProps,
    entityReferences: [],
    droppedWidgets: [{ id: "web_search", type: "tool", label: "Web Search", enabled: true }],
    selectedToolIds: ["web_search"],
  }));
  assert.doesNotMatch(droppedHtml, /data-composer-inline-reference/);
  assert.match(droppedHtml, /border-emerald-600\/50[^>]*>[\s\S]*Web Search/);
});

test("model candidate menu keyboard helpers cycle and select", () => {
  assert.equal(nextModelCandidateIndex(0, 3, 1), 1);
  assert.equal(nextModelCandidateIndex(2, 3, 1), 0);
  assert.equal(nextModelCandidateIndex(0, 3, -1), 2);
  assert.equal(nextModelCandidateIndex(2, 3, -1), 1);
  assert.equal(nextModelCandidateIndex(0, 0, 1), 0);

  assert.deepEqual(modelCandidateMenuKeyAction("Tab", false, 0, 3), {
    handled: true,
    type: "move",
    nextIndex: 1,
  });
  assert.deepEqual(modelCandidateMenuKeyAction("Tab", true, 0, 3), {
    handled: true,
    type: "move",
    nextIndex: 2,
  });
  assert.deepEqual(modelCandidateMenuKeyAction("ArrowDown", false, 1, 3), {
    handled: true,
    type: "move",
    nextIndex: 2,
  });
  assert.deepEqual(modelCandidateMenuKeyAction("ArrowUp", false, 1, 3), {
    handled: true,
    type: "move",
    nextIndex: 0,
  });
  assert.deepEqual(modelCandidateMenuKeyAction("Enter", false, 9, 3), {
    handled: true,
    type: "select",
    index: 2,
  });
  assert.deepEqual(modelCandidateMenuKeyAction("Escape", false, 1, 3), {
    handled: true,
    type: "close",
  });
  assert.deepEqual(modelCandidateMenuKeyAction("Home", false, 1, 3), { handled: false });
  assert.deepEqual(modelCandidateMenuKeyAction("Tab", false, 0, 0), { handled: false });
  assert.deepEqual(modelCandidateMenuKeyAction("Enter", false, 0, 0), { handled: false });
  assert.deepEqual(modelCandidateMenuKeyAction("Escape", false, 0, 0), { handled: false });
});

test("new conversation model dropdown opens below and offset to the right", () => {
  assert.equal(modelDropdownPlacementClassName("below"), "top-full -right-44 mt-2 max-[900px]:right-0");
  assert.equal(modelDropdownPlacementClassName("above"), "bottom-full right-0 mb-2");
});

test("model picker width follows the compact model name only", () => {
  assert.deepEqual(composerModelControlWidth("GPT-5.4"), {
    basis: "9ch",
    min: "5.5rem",
    max: "12rem",
    shrink: 1,
  });
  assert.deepEqual(composerModelControlWidth("GPT 5.4"), composerModelControlWidth("GPT-5.4"));
  assert.deepEqual(composerModelControlWidth("Qwen 3.5 Plus"), {
    basis: "18ch",
    min: "5.5rem",
    max: "12rem",
    shrink: 1,
  });
});

test("model candidate popup anchors to the right edge of the model control", () => {
  assert.deepEqual(
    modelCandidatePopupStyleForAnchor({ left: 820, right: 1010, top: 410 }, 1280),
    {
      left: 550,
      top: 402,
      width: 460,
      transform: "translateY(-100%)",
    },
  );
});

test("model candidate popup stays inside the viewport when anchored near the left edge", () => {
  assert.deepEqual(
    modelCandidatePopupStyleForAnchor({ left: 40, right: 180, top: 210 }, 360),
    {
      left: 8,
      top: 202,
      width: 344,
      transform: "translateY(-100%)",
    },
  );
});

test("model dropdown search supports @provider filters", () => {
  const profiles = [
    {
      profile_id: "google/gemini-2.5-flash",
      qualified_model_id: "google/gemini-2.5-flash",
      display_name: "Gemini 2.5 Flash",
      provider_id: "google",
      provider_display_name: "Google",
      model_id: "gemini-2.5-flash",
    },
    {
      profile_id: "opencode-go/qwen3.5-plus",
      qualified_model_id: "opencode-go/qwen3.5-plus",
      display_name: "Qwen3.5 Plus via OpenCode Go",
      provider_id: "opencode-go",
      provider_display_name: "OpenCode Go",
      model_id: "qwen3.5-plus",
    },
    {
      profile_id: "openai/gpt-4.1",
      qualified_model_id: "openai/gpt-4.1",
      display_name: "GPT 4.1",
      provider_id: "openai",
      provider_display_name: "OpenAI",
      model_id: "gpt-4.1",
    },
  ];

  assert.deepEqual(filterModelProfilesBySearch(profiles, "@google").map((profile) => profile.profile_id), ["google/gemini-2.5-flash"]);
  assert.deepEqual(filterModelProfilesBySearch(profiles, "@opencode qwen").map((profile) => profile.profile_id), ["opencode-go/qwen3.5-plus"]);
  assert.deepEqual(filterModelProfilesBySearch(profiles, "@openai 4.1").map((profile) => profile.profile_id), ["openai/gpt-4.1"]);
});

test("slash key focuses composer only for plain document shortcuts", () => {
  const base = {
    key: "/",
    metaKey: false,
    ctrlKey: false,
    altKey: false,
    defaultPrevented: false,
    isComposing: false,
  };

  assert.equal(shouldFocusComposerForSlashKey(base, null), true);
  assert.equal(shouldFocusComposerForSlashKey({ ...base, key: "a" }, null), false);
  assert.equal(shouldFocusComposerForSlashKey({ ...base, metaKey: true }, null), false);
  assert.equal(shouldFocusComposerForSlashKey({ ...base, defaultPrevented: true }, null), false);
});

test("composer chrome widgets declare layout widths separately from actions", () => {
  assert.deepEqual(
    composerChromeWidgetStyle({ basis: "14rem", min: "11rem", max: "15rem", shrink: 1 }),
    { flex: "0 1 14rem", minWidth: "11rem", maxWidth: "15rem" },
  );

  const html = renderToStaticMarkup(
    createElement(ComposerRenderer, {
      input: "",
      placeholder: "メッセージを入力...",
      isGenerating: false,
      selectedProfile: {
        profile_id: "google/gemini",
        display_name: "Gemini",
        provider_id: "google",
        model_id: "gemini",
        supports_thinking: true,
        thinking_levels: ["high"],
      },
      favoriteProfiles: [],
      inlineExtensions: [],
      belowExtensions: [],
      thinkingLevel: "high",
      contextUsage: { ratio: 0, usedTokens: 0, maxContext: 0, label: "0%" },
      onInputChange: () => undefined,
      onSubmit: () => undefined,
      onModelProfileSelect: () => undefined,
      onThinkingLevelChange: () => undefined,
    }),
  );

  assert.match(html, /data-composer-widget="file-attach"/);
  assert.match(html, /data-composer-widget="model-picker"/);
  assert.match(html, /data-composer-widget="thinking-control"/);
  assert.match(html, /data-composer-widget="send"/);
  assert.match(html, /data-composer-widget="file-attach" data-composer-slot="leading"/);
  assert.match(html, /data-composer-widget="model-picker" data-composer-slot="trailing"/);
  assert.match(html, /style="[^"]*flex:0 1 12ch;min-width:5.5rem;max-width:12rem/);
  assert.match(html, /class="[^"]*rumi-composer-control-surface[^"]*w-full[^"]*gap-2/);
  assert.match(html, /class="[^"]*min-w-0 flex-1 truncate/);
  assert.match(html, /aria-label="Thinking level"/);
  assert.doesNotMatch(html, />thinking</);
});

test("composer renders template-provided slash command suggestions", () => {
  const commands: ComposerCommandItem[] = [
    {
      id: "context_txt",
      name: "context-txt",
      label: "Context TXT",
      description: "Write a context handoff file.",
      category: "tools",
      visibility: "default",
      risk: "low",
      execution: { type: "pack_block", qualified_name: "defaultspack:context_txt.run" },
    },
  ];
  const html = renderToStaticMarkup(
    createElement(ComposerRenderer, {
      input: "/context",
      placeholder: "メッセージを入力...",
      isGenerating: false,
      selectedProfile: {
        profile_id: "stub/default",
        display_name: "Stub Default",
        provider_id: "stub",
        model_id: "default",
      },
      favoriteProfiles: [],
      inlineExtensions: [],
      belowExtensions: [],
      commands,
      thinkingLevel: null,
      contextUsage: { ratio: 0, usedTokens: 0, maxContext: 0, label: "0%" },
      onInputChange: () => undefined,
      onSubmit: () => undefined,
      onCommandSelect: () => undefined,
      onModelProfileSelect: () => undefined,
      onThinkingLevelChange: () => undefined,
    }),
  );

  assert.match(html, /Commands/);
  assert.match(html, /\/context-txt/);
  assert.match(html, /Write a context handoff file/);
});

test("composer suppresses slash command suggestions when template disables slash commands", () => {
  const commands: ComposerCommandItem[] = [
    {
      id: "context_txt",
      name: "context-txt",
      label: "Context TXT",
      description: "Write a context handoff file.",
      category: "tools",
      visibility: "default",
      risk: "low",
      execution: { type: "pack_block", qualified_name: "defaultspack:context_txt.run" },
    },
  ];
  const html = renderToStaticMarkup(
    createElement(ComposerRenderer, {
      input: "/context",
      placeholder: "メッセージを入力...",
      isGenerating: false,
      selectedProfile: {
        profile_id: "stub/default",
        display_name: "Stub Default",
        provider_id: "stub",
        model_id: "default",
      },
      favoriteProfiles: [],
      inlineExtensions: [],
      belowExtensions: [],
      commands,
      composerInput: {
        id: "no_slash_composer",
        feature_flags: { slash_commands: false },
      },
      thinkingLevel: null,
      contextUsage: { ratio: 0, usedTokens: 0, maxContext: 0, label: "0%" },
      onInputChange: () => undefined,
      onSubmit: () => undefined,
      onCommandSelect: () => undefined,
      onModelProfileSelect: () => undefined,
      onThinkingLevelChange: () => undefined,
    }),
  );

  assert.doesNotMatch(html, /Commands/);
  assert.doesNotMatch(html, /\/context-txt/);
  assert.doesNotMatch(html, /Write a context handoff file/);
});

test("composer input template metadata changes safe input copy without replacing the component", () => {
  const html = renderToStaticMarkup(
    createElement(ComposerRenderer, {
      input: "",
      placeholder: "メッセージを入力...",
      isGenerating: false,
      selectedProfile: {
        profile_id: "stub/default",
        display_name: "Stub Default",
        provider_id: "stub",
        model_id: "default",
      },
      favoriteProfiles: [],
      inlineExtensions: [],
      belowExtensions: [],
      composerInput: {
        id: "template.context_txt.input",
        placeholder: "Ask with context.txt in mind",
        help: "Uses template metadata for context handoff prompts.",
        accepted_modalities: ["text", "file"],
        feature_flags: { slash_commands: true, file_attachments: true },
        component: "UntrustedRemoteComposer",
        renderer: "remote-module",
      },
      thinkingLevel: null,
      contextUsage: { ratio: 0, usedTokens: 0, maxContext: 0, label: "0%" },
      onInputChange: () => undefined,
      onSubmit: () => undefined,
      onModelProfileSelect: () => undefined,
      onThinkingLevelChange: () => undefined,
    }),
  );

  assert.match(html, /textarea/);
  assert.match(html, /placeholder="Ask with context\.txt in mind"/);
  assert.match(html, /data-template-composer-input="template\.context_txt\.input"/);
  assert.match(html, /Uses template metadata for context handoff prompts/);
  assert.match(html, />Text</);
  assert.match(html, />Files</);
  assert.match(html, />Slash</);
  assert.doesNotMatch(html, /UntrustedRemoteComposer/);
  assert.doesNotMatch(html, /remote-module/);
});

test("composer renders action approval control and review card", () => {
  const html = renderToStaticMarkup(
    createElement(ComposerRenderer, {
      input: "",
      placeholder: "メッセージを入力...",
      isGenerating: false,
      selectedProfile: {
        profile_id: "stub/default",
        display_name: "Stub Default",
        provider_id: "stub",
        model_id: "default",
      },
      favoriteProfiles: [],
      inlineExtensions: [{ id: "github.search_code", label: "コード検索", category: "tool" }],
      belowExtensions: [],
      thinkingLevel: null,
      contextUsage: { ratio: 0, usedTokens: 0, maxContext: 0, label: "0%" },
      selectedToolIds: ["github.search_code"],
      actionApprovalMode: "ask",
      toolSelectionReview: {
        previewId: "sel_1",
        expiresAt: "2026-01-01T00:05:00Z",
        userText: "GitHubを確認して",
        request: { mode: "review", include: [], exclude: [], scope: "turn", must_use: false },
        createdAt: 1,
        draft: { input: "GitHubを確認して", attachments: [], droppedWidgets: [] },
        decision: {
          selected_tools: ["github.search_code"],
          selected_services: [{ service_id: "github", label: "GitHub", tool_count: 1 }],
          recommendations: [{ tool_id: "github.search_code", reason: "対象実装を確認するため" }],
          permission_summary: { auto: 1, confirm: 0, block: 0 },
        },
      },
      onInputChange: () => undefined,
      onSubmit: () => undefined,
      onModelProfileSelect: () => undefined,
      onThinkingLevelChange: () => undefined,
      onToolSelectionReviewApprove: () => undefined,
      onToolSelectionReviewEdit: () => undefined,
      onToolSelectionReviewNoTools: () => undefined,
      onToolSelectionReviewCancel: () => undefined,
    }),
  );

  assert.match(html, /data-composer-widget="action-approval-control"/);
  assert.match(html, /アクションの承認方法/);
  assert.match(html, /承認/);
  assert.match(html, /使用する機能を確認/);
  assert.match(html, /この内容で続ける/);
});

test("new conversation composer input is not locked to one visual line", () => {
  const html = renderToStaticMarkup(
    createElement(ComposerRenderer, {
      input: "first line\nsecond line",
      placeholder: "メッセージを入力...",
      isGenerating: false,
      isNewConversation: true,
      selectedProfile: {
        profile_id: "openai/gpt-5.5",
        display_name: "GPT-5.5",
        provider_id: "openai",
        model_id: "gpt-5.5",
        supports_vision: true,
        supports_tool_calling: true,
        supports_thinking: true,
        thinking_levels: ["high"],
      },
      favoriteProfiles: [],
      inlineExtensions: [],
      belowExtensions: [],
      thinkingLevel: "high",
      contextUsage: { ratio: 0, usedTokens: 0, maxContext: 0, label: "0%" },
      onInputChange: () => undefined,
      onSubmit: () => undefined,
      onModelProfileSelect: () => undefined,
      onThinkingLevelChange: () => undefined,
    }),
  );

  assert.doesNotMatch(html, /rumi-composer-input-new-overlay/);
  assert.match(html, /rumi-composer-input-new[^"]*min-h-\[24px\]/);
  assert.match(html, /rumi-composer-input-new[^"]*max-h-\[150px\]/);
  assert.match(html, /rumi-composer-input-new[^"]*text-zinc-100/);
  assert.doesNotMatch(html, /rumi-composer-input-new[^"]*text-transparent/);
  assert.doesNotMatch(html, /rumi-composer-input-new[^"]*\sh-\[22px\]/);
  assert.match(html, /style="[^"]*flex:0 1 9ch;min-width:5.5rem;max-width:12rem/);
});

test("composer renders model status indicators beside the model picker", () => {
  const html = renderToStaticMarkup(
    createElement(ComposerRenderer, {
      input: "",
      placeholder: "メッセージを入力...",
      isGenerating: false,
      selectedProfile: {
        profile_id: "google/gemini",
        display_name: "Gemini",
        provider_id: "google",
        model_id: "gemini",
        supports_thinking: true,
        thinking_levels: ["high"],
      },
      favoriteProfiles: [],
      inlineExtensions: [],
      belowExtensions: [],
      thinkingLevel: "high",
      contextUsage: { ratio: 0, usedTokens: 0, maxContext: 0, label: "0%" },
      modelStatusIndicators: [
        {
          id: "yolo",
          name: "YOLO",
          description: "YOLO が ON です。",
          svgMarkup: "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 100 100\" width=\"100\" height=\"100\"><circle cx=\"50\" cy=\"50\" r=\"40\" fill=\"#fca355\" /></svg>",
          tone: "warning",
          action: {
            label: "標準に戻す",
            onSelect: () => undefined,
          },
        },
      ],
      yoloMode: true,
      onInputChange: () => undefined,
      onSubmit: () => undefined,
      onModelProfileSelect: () => undefined,
      onThinkingLevelChange: () => undefined,
    }),
  );

  assert.match(html, /aria-label="YOLO"/);
  assert.match(html, /title="YOLO が ON です。"/);
  assert.match(html, /viewBox="0 0 100 100"/);
  assert.match(html, /data-composer-widget="model-picker"/);
  assert.match(html, /data-composer-widget="thinking-control"/);
  assert.match(html, /data-composer-widget="model-status"/);
  assert.ok(html.indexOf('data-composer-widget="model-picker"') < html.indexOf('data-composer-widget="thinking-control"'));
  assert.ok(html.indexOf('data-composer-widget="thinking-control"') < html.indexOf('data-composer-widget="model-status"'));
  assert.doesNotMatch(html, /data-composer-widget="yolo-status"/);
});

test("composer model drop selects the model instead of creating a widget chip", () => {
  const action = resolveComposerWidgetDrop(
    { id: "openai/gpt-4.1", type: "model", label: "GPT 4.1" },
    [],
  );

  assert.deepEqual(action, { type: "select_model", profileId: "openai/gpt-4.1" });
});

test("composer uses the main input as steer while generating", () => {
  const html = renderToStaticMarkup(
    createElement(ComposerRenderer, {
      input: "次は短くして",
      placeholder: "メッセージを入力...",
      isGenerating: true,
      selectedProfile: {
        profile_id: "stub/default",
        display_name: "Stub Default",
        provider_id: "stub",
        model_id: "default",
      },
      favoriteProfiles: [],
      inlineExtensions: [],
      belowExtensions: [],
      thinkingLevel: null,
      contextUsage: { ratio: 0, usedTokens: 0, maxContext: 0, label: "0%" },
      onInputChange: () => undefined,
      onSubmit: () => undefined,
      onModelProfileSelect: () => undefined,
      onThinkingLevelChange: () => undefined,
      onSteerSubmit: () => undefined,
    }),
  );

  assert.match(html, /追加の指示を入力/);
  assert.match(html, /Enterで追加指示を送信/);
  assert.match(html, /title="追加指示を送る"/);
  assert.doesNotMatch(html, /実行中のAIへステアを入力/);
  assert.doesNotMatch(html, /AI実行中/);
  assert.doesNotMatch(html, /textarea[^>]*disabled/);
  assert.doesNotMatch(html, /これがステア/);
  assert.doesNotMatch(html, /フォローアップの変更を求める/);
});

test("composer renders the current steer above the main input", () => {
  const html = renderToStaticMarkup(
    createElement(ComposerRenderer, {
      input: "",
      placeholder: "メッセージを入力...",
      isGenerating: true,
      selectedProfile: {
        profile_id: "stub/default",
        display_name: "Stub Default",
        provider_id: "stub",
        model_id: "default",
      },
      favoriteProfiles: [],
      inlineExtensions: [],
      belowExtensions: [],
      thinkingLevel: null,
      contextUsage: { ratio: 0, usedTokens: 0, maxContext: 0, label: "0%" },
      steerStatus: "ステアを反映しました",
      steerPreviewItems: [
        {
          id: "steer_1",
          prompt: "結論を先にして、短く返して",
          status: "injected",
          visible: true,
        },
      ],
      onInputChange: () => undefined,
      onSubmit: () => undefined,
      onModelProfileSelect: () => undefined,
      onThinkingLevelChange: () => undefined,
      onSteerSubmit: () => undefined,
    }),
  );

  assert.doesNotMatch(html, /これがステア/);
  assert.match(html, /反映済み/);
  assert.match(html, /結論を先にして、短く返して/);
  assert.doesNotMatch(html, /フォローアップの変更を求める/);
});

test("vision unsupported banner appears when image input exists and selected model lacks vision", () => {
  const html = renderToStaticMarkup(
    createElement(ComposerRenderer, {
      input: "",
      placeholder: "メッセージを入力...",
      isGenerating: false,
      selectedProfile: {
        profile_id: "stub/default",
        display_name: "Stub Default",
        provider_id: "stub",
        model_id: "default",
        supports_vision: false,
        supports_image_input: false,
      },
      favoriteProfiles: [],
      inlineExtensions: [],
      belowExtensions: [],
      thinkingLevel: null,
      contextUsage: { ratio: 0, usedTokens: 0, maxContext: 0, label: "0%" },
      attachedFiles: [{
        id: "img-1",
        name: "tiny.png",
        size: 68,
        type: "image/png",
        dataUrl: "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMB/axR4xUAAAAASUVORK5CYII=",
      }],
      onInputChange: () => undefined,
      onSubmit: () => undefined,
      onModelProfileSelect: () => undefined,
      onThinkingLevelChange: () => undefined,
      onOpenModelManager: () => undefined,
      onOpenToolSettings: () => undefined,
      onSwitchToVisionModel: () => undefined,
    }),
  );

  assert.match(html, /現在のモデルはVision非対応です/);
  assert.match(html, /Visionモデルへ切替/);
  assert.match(html, /Model設定/);
});

test("composer asks for an API key when an unconfigured Gemini model is selected", () => {
  assert.equal(profileNeedsApiKey({
    profile_id: "google/gemini-2.5-flash",
    display_name: "Gemini 2.5 Flash",
    provider_id: "google",
    model_id: "gemini-2.5-flash",
    availability: { configured: false, status: "catalog" },
  }), true);

  assert.equal(profileNeedsApiKey({
    profile_id: "google/gemma-4-26b-a4b-it",
    display_name: "Gemma 4 26B A4B IT",
    provider_id: "google",
    model_id: "gemma-4-26b-a4b-it",
    availability: { configured: false, status: "catalog" },
  }), true);

  assert.equal(profileNeedsApiKey({
    profile_id: "google/gemini-2.5-flash",
    display_name: "Gemini 2.5 Flash",
    provider_id: "google",
    model_id: "gemini-2.5-flash",
    availability: { configured: true, status: "configured" },
  }), false);

  assert.equal(profileNeedsApiKey({
    profile_id: "openai/gpt-5.5",
    display_name: "GPT-5.5",
    provider_id: "openai",
    model_id: "gpt-5.5",
    availability: { configured: false, status: "catalog" },
  }), true);

  assert.equal(profileNeedsApiKey({
    profile_id: "stub/default",
    display_name: "Stub Default",
    provider_id: "stub",
    model_id: "default",
  }), false);

  assert.equal(profileNeedsApiKey({
    profile_id: "ollama/llama3.2",
    display_name: "Llama 3.2",
    provider_id: "ollama",
    model_id: "llama3.2",
    availability: { configured: false, local: true, status: "catalog" },
  }), false);
});

test("composer widget drop requires explicit kind capability contract", () => {
  const toolItems = [
    {
      id: "coding_file_read",
      label: "Read File",
      ui: { widget_kind: "tool_toggle", drop_capabilities: [COMPOSER_TOGGLE_DROP] },
    },
    {
      id: "git_status",
      label: "Git Status",
      ui: { widget_kind: "button", drop_capabilities: [COMPOSER_BUTTON_DROP] },
    },
    {
      id: "provider-catalog",
      label: "Providers",
      ui: { widget_kind: "panel", drop_capabilities: [COMPOSER_PANEL_DROP] },
    },
    {
      id: "model-selector",
      label: "Model Selector",
      ui: { widget_kind: "selector", drop_capabilities: [COMPOSER_SELECTOR_DROP] },
    },
    {
      id: "bad-panel",
      label: "Bad Panel",
      ui: { widget_kind: "panel", drop_capabilities: [COMPOSER_TOGGLE_DROP] },
    },
  ];

  assert.equal(resolveComposerWidgetDrop({ id: "coding_file_read", type: "tool", label: "Read" }, toolItems).type, "drop_widget");
  assert.equal(resolveComposerWidgetDrop({ id: "git_status", type: "button", label: "Git", widgetKind: "button" }, toolItems).type, "drop_widget");
  assert.equal(resolveComposerWidgetDrop({ id: "provider-catalog", type: "panel", label: "Providers", widgetKind: "panel" }, toolItems).type, "drop_widget");
  assert.equal(resolveComposerWidgetDrop({ id: "model-selector", type: "selector", label: "Models", widgetKind: "selector" }, toolItems).type, "drop_widget");
  assert.equal(resolveComposerWidgetDrop({ id: "bad-panel", type: "panel", label: "Bad", widgetKind: "panel" }, toolItems).type, "ignore");
  assert.equal(resolveComposerWidgetDrop({ id: "unknown", type: "button", label: "Unknown" }, toolItems).type, "ignore");
});

test("coding workspace picker renders selected workspace and trust affordance", () => {
  const html = renderToStaticMarkup(
    createElement(CodingWorkspacePicker, {
      workspaces: [
        { workspace_id: "ws1", label: "Main Repo", root_path: "/repo", trusted: false },
      ],
      selectedWorkspaceId: "ws1",
      onSelect: () => undefined,
      onTrust: () => undefined,
      onRefresh: () => undefined,
    }),
  );

  assert.match(html, /Main Repo/);
  assert.match(html, /ShieldQuestion|text-amber-300/);
  assert.match(html, /rumi-workspace-picker-action is-trust/);
  assert.match(html, /aria-label="Main Repo を信頼"/);
});

test("composer copy resolver suppresses internal template implementation copy", () => {
  assert.equal(composerPlaceholderCopy({
    isSteerMode: false,
    mode: "chat",
    placeholder: "メッセージを入力...",
    templatePlaceholder: "メッセージを入力... /context text で会話をTXT化",
  }), "メッセージを入力...");
  assert.equal(composerPlaceholderCopy({
    isSteerMode: true,
    mode: "chat",
  }), "追加の指示を入力");
  assert.equal(composerHelperCopy({
    isSteerMode: false,
    hasInput: false,
    slashCommands: false,
    atMentions: false,
    fileAttachments: true,
    templateHelp: "Template-composed composer: slash commands, mentions, files",
  }), "Enterで送信 · ファイル添付対応");
});
