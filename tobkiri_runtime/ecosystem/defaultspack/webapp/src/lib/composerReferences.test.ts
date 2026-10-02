import test from "node:test";
import assert from "node:assert/strict";

import {
  composerReferencesAsMarkdown,
  insertComposerReferencePaste,
  mergeComposerReferences,
  restoreComposerMarkdownReferences,
  restoreComposerReferences,
  serializeComposerReferences,
  type ComposerEntityReference,
} from "./composerReferences";

const tools = [{ id: "web_search", label: "Web Search", category: "tool" }];
const skills = [{ id: "feedback/live-review", label: "Live Review", metadata: { revision: 3 } }];

test("composer references serialize and restore the selected entities", () => {
  const text = "Use @web_search with @feedback/live-review.";
  const references: ComposerEntityReference[] = [
    { kind: "tool", id: "web_search", syntax: "@web_search" },
    { kind: "skill", id: "feedback/live-review", syntax: "@feedback/live-review" },
  ];
  const serialized = serializeComposerReferences(text, references);
  assert.ok(serialized);
  assert.deepEqual(restoreComposerReferences(serialized, { tools, skills }), { text, references });
});

test("composer references preserve display labels in custom clipboard data", () => {
  const text = "Use @Web Search";
  const references: ComposerEntityReference[] = [
    { kind: "tool", id: "web_search", syntax: "@Web Search" },
  ];
  const serialized = serializeComposerReferences(text, references);
  assert.ok(serialized);
  assert.deepEqual(restoreComposerReferences(serialized, { tools, skills }), { text, references });
});

test("composer references use portable Codex-style markdown on the plain-text clipboard", () => {
  const text = "Use @Web Search now";
  const references: ComposerEntityReference[] = [
    { kind: "tool", id: "web_search", syntax: "@Web Search" },
  ];
  assert.equal(
    composerReferencesAsMarkdown(text, references),
    "Use [@Web Search](plugin://web_search) now",
  );
});

test("Codex-style plugin mention paste restores installed semantic tools", () => {
  assert.deepEqual(
    restoreComposerMarkdownReferences(
      'Ask [@Web Search](plugin://web_search@openai-bundled") now',
      { tools, skills },
    ),
    {
      text: "Ask @Web Search now",
      references: [{ kind: "tool", id: "web_search", syntax: "@Web Search" }],
    },
  );
});

test("unknown Codex-style plugin links paste as readable plain mentions", () => {
  assert.deepEqual(
    restoreComposerMarkdownReferences(
      "Ask [@Missing](plugin://missing@openai-bundled) now",
      { tools, skills },
    ),
    { text: "Ask @Missing now", references: [] },
  );
});

test("unknown pasted references remain plain text", () => {
  const text = "Ask @removed_tool for help";
  const serialized = serializeComposerReferences(text, [{ kind: "tool", id: "removed_tool", syntax: "@removed_tool" }]);
  assert.ok(serialized);
  const restored = restoreComposerReferences(serialized, { tools, skills });
  assert.deepEqual(restored, { text, references: [] });
  assert.deepEqual(insertComposerReferencePaste("Before after", 7, 7, restored!), {
    value: "Before Ask @removed_tool for helpafter",
    cursor: 33,
    references: [],
  });
});

test("reference paste replaces the selection and keeps resolved entity identity", () => {
  const restored = {
    text: "@web_search",
    references: [{ kind: "tool", id: "web_search", syntax: "@web_search" } satisfies ComposerEntityReference],
  };
  assert.deepEqual(insertComposerReferencePaste("Use old now", 4, 7, restored), {
    value: "Use @web_search now",
    cursor: 15,
    references: restored.references,
  });
});

test("reference state drops entities whose syntax was edited away", () => {
  const references: ComposerEntityReference[] = [
    { kind: "tool", id: "web_search", syntax: "@web_search" },
    { kind: "skill", id: "feedback/live-review", syntax: "@feedback/live-review" },
  ];
  assert.deepEqual(mergeComposerReferences(references, [], "Only @web_search remains"), [references[0]]);
});

test("reference state drops entities whose syntax is escaped", () => {
  const reference = { kind: "tool", id: "web_search", syntax: "@Web Search" } satisfies ComposerEntityReference;
  assert.deepEqual(mergeComposerReferences([reference], [], "Use \\@Web Search literally"), []);
});

test("malformed clipboard reference data is ignored", () => {
  assert.equal(restoreComposerReferences("not json", { tools, skills }), null);
  assert.equal(restoreComposerReferences(JSON.stringify({ version: 1, text: "@x", references: [{ kind: "tool", id: "x", start: 0, end: 99 }] }), { tools, skills }), null);
});

test("forged clipboard labels cannot activate a known entity", () => {
  const text = "@harmless_text";
  const forged = JSON.stringify({
    version: 1,
    text,
    references: [{ kind: "tool", id: "web_search", start: 0, end: text.length }],
  });
  assert.deepEqual(restoreComposerReferences(forged, { tools, skills }), { text, references: [] });
});
