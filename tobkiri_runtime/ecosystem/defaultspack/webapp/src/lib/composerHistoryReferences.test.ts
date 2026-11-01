import test from "node:test";
import assert from "node:assert/strict";
import { composerReferencesAsMarkdown, mergeComposerReferences, restoreComposerReferences, serializeComposerReferences, type ComposerEntityReference } from "./composerReferences";

const group: ComposerEntityReference = { kind: "group", id: "g-one", syntax: "@Same", profileId: "profile-a", label: "Same", memberIds: ["chat-a", "chat-b"] };
const catalog = { tools: [], skills: [], profileId: "profile-a", historyReferences: [group] };

test("history clipboard requires exact same-Profile resolved catalogue", () => {
  const raw = serializeComposerReferences("@Same", [group])!;
  assert.deepEqual(restoreComposerReferences(raw, catalog)?.references, [group]);
  assert.deepEqual(restoreComposerReferences(raw, { tools: [], skills: [] })?.references, []);
  assert.deepEqual(restoreComposerReferences(raw, { ...catalog, profileId: "profile-b" })?.references, []);
});

test("clipboard membership never overwrites confirmed catalogue membership", () => {
  const raw = JSON.parse(serializeComposerReferences("@Same", [group])!);
  raw.references[0].memberIds = ["injected"];
  raw.references[0].label = "injected";
  assert.deepEqual(restoreComposerReferences(JSON.stringify(raw), catalog)?.references, [group]);
});

test("same labels preserve distinct IDs and Profiles", () => {
  const other = { ...group, id: "g-two", memberIds: ["chat-c"] };
  const merged = mergeComposerReferences([], [group, other], "@Same @Same");
  assert.equal(merged.length, 2);
  const raw = serializeComposerReferences("@Same @Same", merged)!;
  assert.equal(restoreComposerReferences(raw, { ...catalog, historyReferences: merged })?.references.length, 2);
  assert.equal(mergeComposerReferences([], [group, { ...group, profileId: "profile-b" }], "@Same").length, 2);
});

test("plain typing alone creates no history semantics", () => {
  assert.deepEqual(mergeComposerReferences([], [], "@chat @Same"), []);
  assert.deepEqual(mergeComposerReferences([group], [], "\\@Same"), []);
});


test("history Markdown carries literal canonical kind and identity without plugin links", () => {
  assert.equal(composerReferencesAsMarkdown("@Same", [group]), "@group:g-one");
  const chat = { ...group, kind: "chat" as const, id: "chat-one" };
  assert.equal(composerReferencesAsMarkdown("@Same", [chat]), "@chat:chat-one");
});
