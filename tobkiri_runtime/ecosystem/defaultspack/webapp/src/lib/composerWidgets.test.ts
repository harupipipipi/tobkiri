function routeKey(path: string): string {
  return `/${path}`;
}

import test from "node:test";
import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import { resolve } from "node:path";

import {
  canExecuteComposerEndpointAction,
  composerMentionMetadataFromWidgets,
  composerFileMentionWidget,
  composerServiceMentionWidget,
  composerSkillMentionDisplay,
  composerSkillMentionWidget,
  composerToolMentionDisplay,
  composerToolMentionWidget,
  filterComposerSkillMentions,
  filterComposerToolMentions,
  isSafeLocalEndpoint,
  reconcileComposerSemanticDraft,
  resolveComposerWidgetDrop,
  skillMentionIdsFromText,
  toolMentionIdsFromText,
  trustedComposerActionForWidget,
  withComposerMentionSelectionOwnership,
} from "./composerWidgets";
import {
  activeMentionAtCursor,
  codePointIndexToUtf16Offset,
  extractMentionTokens,
  hasUnescapedMentionSyntax,
  utf16OffsetToCodePointIndex,
} from "./mentionContract";
import type { ComposerExtensionItem } from "../renderers/types";
import {
  initialComposerFieldValues,
  normalizeComposerFields,
  structuredComposerPayload,
} from "./structuredComposer";

type BoundaryFixture = {
  active_query: string | null;
  active_start_codepoint?: number;
  active_start_utf16?: number;
  cursor_codepoint?: number;
  cursor_utf16?: number;
  known_values?: string[];
  name: string;
  text: string;
  token_spans?: Array<[number, number]>;
  tokens: string[];
};

const sharedBoundaryFixturePath = resolve(
  import.meta.dirname,
  "../../../../..",
  "tests/fixtures/mention_boundaries.json",
);
const packagedBoundaryFixturePath = resolve(import.meta.dirname, "fixtures/mention_boundaries.json");
const boundaryFixtures = JSON.parse(readFileSync(
  existsSync(sharedBoundaryFixturePath) ? sharedBoundaryFixturePath : packagedBoundaryFixturePath,
  "utf8",
)) as BoundaryFixture[];

test("frontend follows the shared Unicode mention boundary fixtures", () => {
  for (const fixture of boundaryFixtures) {
    assert.deepEqual(
      extractMentionTokens(fixture.text, fixture.known_values).map((mention) => mention.value),
      fixture.tokens,
      fixture.name,
    );
    if (fixture.token_spans) {
      assert.deepEqual(
        extractMentionTokens(fixture.text, fixture.known_values).map(({ start, end }) => [start, end]),
        fixture.token_spans,
        fixture.name,
      );
    }
    const cursor = fixture.cursor_utf16 ?? fixture.text.length;
    const activeMention = activeMentionAtCursor(fixture.text, cursor, fixture.known_values);
    assert.equal(
      activeMention?.query ?? null,
      fixture.active_query,
      fixture.name,
    );
    if (fixture.cursor_codepoint !== undefined) {
      assert.equal(utf16OffsetToCodePointIndex(fixture.text, cursor), fixture.cursor_codepoint);
      assert.equal(codePointIndexToUtf16Offset(fixture.text, fixture.cursor_codepoint), cursor);
      assert.equal(activeMention?.start, fixture.active_start_utf16);
      assert.equal(activeMention?.startCodePoint, fixture.active_start_codepoint);
    }
  }
});

test("composer endpoint actions are limited to safe local non-approval APIs", () => {
  assert.equal(isSafeLocalEndpoint(routeKey("api/coding/git/status")), true);
  assert.equal(isSafeLocalEndpoint("//evil.example/api"), false);
  assert.equal(isSafeLocalEndpoint("https://evil.example/api"), false);
  assert.equal(isSafeLocalEndpoint("/not-api/status"), false);

  assert.equal(
    canExecuteComposerEndpointAction({
      type: "call_endpoint",
      endpoint: routeKey("api/coding/git/status"),
      requires_approval: false,
    }),
    true,
  );
  assert.equal(
    canExecuteComposerEndpointAction({
      type: "call_endpoint",
      endpoint: routeKey("api/coding/files/write"),
      requires_approval: true,
    }),
    false,
  );
  assert.equal(
    canExecuteComposerEndpointAction({
      type: "call_endpoint",
      endpoint: routeKey("api/ui/settings"),
      method: "PUT",
      requires_approval: false,
    }),
    false,
  );
});

test("composer widget drops rebuild actions from trusted catalog items", () => {
  const toolItems = [
    {
      id: "coding_git_status",
      label: "Git Status",
      category: "tool",
      ui: {
        drop_capabilities: ["composer.action_button"],
        widget_kind: "button",
        composer_label: "Git Status",
        composer_icon: "git",
        composer_action: {
          type: "call_endpoint",
          endpoint: routeKey("api/coding/git/status"),
          method: "GET",
          result_surface: "preview",
          requires_approval: false,
        },
      },
    },
  ] satisfies ComposerExtensionItem[];

  const action = resolveComposerWidgetDrop({
    id: "coding_git_status",
    sourceItemId: "coding_git_status",
    type: "button",
    label: "Forged Git",
    widgetKind: "button",
    action: {
      type: "call_endpoint",
      endpoint: routeKey("api/ui/settings"),
      method: "PUT",
      payload: { values: { yolo_mode: true } },
      requires_approval: false,
    },
  }, toolItems);

  assert.equal(action.type, "drop_widget");
  if (action.type !== "drop_widget") return;
  assert.equal(action.widget.label, "Git Status");
  assert.deepEqual(action.widget.action, toolItems[0].ui.composer_action);
  assert.equal(trustedComposerActionForWidget(action.widget, toolItems), toolItems[0].ui.composer_action);
});

test("composer skill mentions resolve aliases and create prompt widgets", () => {
  const skills = [
    {
      id: "feedback/live-review",
      label: "Live Review",
      description: "Require evidence-backed verification.",
      triggers: ["PR97_LIVE_REALITY_REVIEW"],
      appliesToTools: ["browser_use"],
      aliases: ["reality"],
    },
  ];

  assert.deepEqual(filterComposerSkillMentions(skills, "evidence").map((skill) => skill.id), ["feedback/live-review"]);
  assert.deepEqual(skillMentionIdsFromText("Use @live-review and @reality.", skills), ["feedback/live-review"]);
  assert.deepEqual(composerSkillMentionWidget(skills[0]), {
    id: "feedback/live-review",
    type: "skill",
    label: "Live Review",
    enabled: true,
    widgetKind: "skill_prompt",
    sourceItemId: "feedback/live-review",
    description: "Require evidence-backed verification.",
    metadata: {
      source: "composer_at_mention",
      mention: {
        id: "feedback/live-review",
        kind: "skill",
        label: "Live Review",
        syntax: "@Live Review",
        skill_id: "feedback/live-review",
      },
      skill: {
        id: "feedback/live-review",
        label: "Live Review",
        description: "Require evidence-backed verification.",
        triggers: ["PR97_LIVE_REALITY_REVIEW"],
        applies_to_tools: ["browser_use"],
        aliases: ["reality"],
      },
    },
  });
});

test("composer mention display keeps internal ids out of normal UI", () => {
  assert.deepEqual(composerToolMentionDisplay({
    id: "coding_file_read",
    label: "Read File",
    category: "tool",
    description: "Read a workspace file.",
  }), {
    label: "Read File",
    description: "Read a workspace file.",
  });
  assert.deepEqual(composerSkillMentionDisplay({
    id: "feedback/live-review",
    label: "Live Review",
    description: "Require evidence-backed verification.",
  }), {
    label: "Live Review",
    description: "Require evidence-backed verification.",
  });
});

test("composer tool mentions rank Calculator ahead of description-only matches", () => {
  const tools = [
    {
      id: "chart_create",
      label: "Chart Create",
      category: "tool",
      description: "Create charts from calculated values.",
    },
    {
      id: "sheet_create",
      label: "Sheet Create",
      category: "tool",
      description: "Create a sheet and recalculate formulas.",
    },
    {
      id: "sheet_read",
      label: "Sheet Read",
      category: "tool",
      description: "Read calculation results from a sheet.",
    },
    {
      id: "calculator",
      label: "Calculator",
      category: "tool",
      description: "Run arithmetic.",
      tags: ["math"],
    },
  ] satisfies ComposerExtensionItem[];

  for (const query of ["calc", "CALC", " calc ", "ＣＡＬＣ"]) {
    assert.deepEqual(
      filterComposerToolMentions(tools, query).map((tool) => tool.id),
      ["calculator", "chart_create", "sheet_create", "sheet_read"],
      query,
    );
  }
  assert.deepEqual(
    filterComposerToolMentions(tools, "calculator").map((tool) => tool.id),
    ["calculator"],
  );
});

test("composer tool mention ranking preserves safe catalog-search fallbacks", () => {
  const tools = [
    {
      id: "chart_create",
      label: "Chart Create",
      category: "tool",
      description: "Create charts from calculation results.",
      tags: ["visualization"],
    },
    {
      id: "sheet_update",
      label: "Sheet Update",
      category: "tool",
      description: "Update calculation inputs, units, and currency columns.",
      tags: ["spreadsheet"],
    },
    {
      id: "calculator",
      label: "Calculator",
      category: "tool",
      description: "Run exact arithmetic with negative and decimal values.",
      tags: ["math"],
    },
  ] satisfies ComposerExtensionItem[];

  const goldenCases = [
    { query: "chart", expected: ["chart_create"] },
    { query: "spread", expected: ["sheet_update"] },
    { query: "calculation", expected: ["chart_create", "sheet_update"] },
    { query: "unit", expected: ["sheet_update"] },
    { query: "currency", expected: ["sheet_update"] },
    { query: "negative", expected: ["calculator"] },
    { query: "decimal", expected: ["calculator"] },
    { query: "2+2", expected: [] },
    { query: "convert 5 usd", expected: [] },
    { query: "カレンダー", expected: [] },
  ];

  for (const { query, expected } of goldenCases) {
    assert.deepEqual(
      filterComposerToolMentions(tools, query).map((tool) => tool.id),
      expected,
      query,
    );
  }
  assert.deepEqual(
    filterComposerToolMentions(tools, "").map((tool) => tool.id),
    ["chart_create", "sheet_update", "calculator"],
  );
  assert.deepEqual(
    filterComposerToolMentions([{ ...tools[2], disabled: true }], "calc"),
    [],
  );
});

test("semantic mention metadata keeps stable ids separate from human labels", () => {
  const fileWidget = composerFileMentionWidget("src/App.tsx");
  const serviceWidget = composerServiceMentionWidget({
    id: "github",
    label: "GitHub",
    toolIds: ["github_issue_search"],
  });
  assert.deepEqual(composerMentionMetadataFromWidgets([fileWidget, serviceWidget]), [
    {
      id: "src/App.tsx",
      kind: "file",
      label: "src/App.tsx",
      syntax: "@src/App.tsx",
    },
    {
      id: "github",
      kind: "service",
      label: "GitHub",
      syntax: "@GitHub",
    },
  ]);
});

test("semantic mention reconciliation removes escaped and deselected tool state", () => {
  const tool = { id: "web_search", label: "Web Search", category: "tool" };
  const ownedWidget = withComposerMentionSelectionOwnership(
    composerToolMentionWidget(tool),
    [],
  );

  assert.deepEqual(reconcileComposerSemanticDraft({
    droppedWidgets: [ownedWidget],
    selectedToolIds: ["web_search"],
    text: "Use \\@Web Search",
  }), {
    droppedWidgets: [],
    selectedToolIds: [],
  });
  assert.deepEqual(reconcileComposerSemanticDraft({
    droppedWidgets: [ownedWidget],
    selectedToolIds: [],
    text: "Use @Web Search",
  }), {
    droppedWidgets: [],
    selectedToolIds: [],
  });
});

test("semantic mention matching requires a complete token boundary", () => {
  assert.equal(hasUnescapedMentionSyntax("Use @GitHub", "@Git"), false);
  assert.equal(hasUnescapedMentionSyntax("Use @Git/foo", "@Git"), false);
  assert.equal(hasUnescapedMentionSyntax("Use @Git.", "@Git"), true);
  assert.equal(hasUnescapedMentionSyntax("Use @Git, please", "@Git"), true);

  const widget = withComposerMentionSelectionOwnership(
    composerToolMentionWidget({ id: "git", label: "Git", category: "tool" }),
    [],
  );
  assert.deepEqual(reconcileComposerSemanticDraft({
    droppedWidgets: [widget],
    selectedToolIds: ["git"],
    text: "Use @GitHub",
  }), {
    droppedWidgets: [],
    selectedToolIds: [],
  });
});

test("semantic mention reconciliation preserves a pre-existing manual tool choice", () => {
  const widget = withComposerMentionSelectionOwnership(
    composerToolMentionWidget({ id: "web_search", label: "Web Search", category: "tool" }),
    ["web_search"],
  );

  assert.deepEqual(reconcileComposerSemanticDraft({
    droppedWidgets: [widget],
    selectedToolIds: ["web_search"],
    text: "literal \\@Web Search",
  }), {
    droppedWidgets: [],
    selectedToolIds: ["web_search"],
  });
});

test("semantic mention reconciliation requires live file attachments and service tools", () => {
  const fileWidget = composerFileMentionWidget("README.md");
  const serviceWidget = withComposerMentionSelectionOwnership(composerServiceMentionWidget({
    id: "github",
    label: "GitHub",
    toolIds: ["github_issue_search"],
  }), []);

  assert.deepEqual(reconcileComposerSemanticDraft({
    attachmentPaths: [],
    droppedWidgets: [fileWidget],
    requireFileAttachment: true,
    selectedToolIds: [],
    text: "Review @README.md",
  }).droppedWidgets, []);
  assert.deepEqual(reconcileComposerSemanticDraft({
    attachmentPaths: ["README.md"],
    droppedWidgets: [fileWidget],
    requireFileAttachment: true,
    selectedToolIds: [],
    text: "Review @README.md",
  }).droppedWidgets, [fileWidget]);
  assert.deepEqual(reconcileComposerSemanticDraft({
    droppedWidgets: [serviceWidget],
    selectedToolIds: [],
    text: "Use @GitHub",
  }), {
    droppedWidgets: [],
    selectedToolIds: [],
  });
});

test("duplicate human labels are not ambiguously reparsed", () => {
  const tools = [
    { id: "browser_computer", label: "Browser", category: "tool" },
    { id: "browser_companion", label: "Browser", category: "tool" },
  ];
  assert.deepEqual(skillMentionIdsFromText("@Browser", []), []);
  assert.deepEqual(toolMentionIdsFromText("@Browser", tools), []);
  assert.deepEqual(toolMentionIdsFromText("@browser_computer", tools), ["browser_computer"]);
  assert.deepEqual(composerMentionMetadataFromWidgets([
    composerToolMentionWidget(tools[1]),
  ]), [{
    id: "browser_companion",
    kind: "tool",
    label: "Browser",
    syntax: "@Browser",
  }]);
});

test("copy and paste keeps human mention text literal without exposing an internal id", () => {
  const tools = [{ id: "browser_computer", label: "Browser Computer", category: "tool" }];
  const pastedText = "Use @Browser Computer";

  assert.equal(pastedText.includes("browser_computer"), false);
  assert.deepEqual(toolMentionIdsFromText(pastedText, tools), []);
});

test("structured composer normalizes pack JSON fields and select defaults", () => {
  const fields = normalizeComposerFields([
    { id: "intent", label: "Intent", type: "select", options: ["Plan", { value: "build", label: "Build" }] },
    { id: "notes", type: "text", default: "short" },
    { id: "broken", type: "select", options: [] },
  ]);

  assert.deepEqual(fields.map((field) => field.id), ["intent", "notes"]);
  assert.deepEqual(initialComposerFieldValues(fields), { intent: "Plan", notes: "short" });
});

test("structured composer keeps field JSON separate and omits empty values", () => {
  const fields = normalizeComposerFields([
    { id: "output", type: "select", options: ["summary", "code"] },
    { id: "detail", type: "textarea" },
  ]);

  assert.deepEqual(
    structuredComposerPayload(fields, { output: "summary", detail: "" }),
    { output: "summary" },
  );
});
