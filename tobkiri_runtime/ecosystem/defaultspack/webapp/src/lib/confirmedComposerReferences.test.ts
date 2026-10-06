import test from "node:test";
import assert from "node:assert/strict";
import { confirmedComposerSkillIds } from "./confirmedComposerReferences";
import { composerSkillMentionWidget } from "./composerWidgets";

const skills = [{ id: "settings", label: "Settings" }];

test("typed skill syntax has no semantic effect", () => {
  assert.deepEqual(confirmedComposerSkillIds("@Settings", [], [], skills), []);
});

test("confirmed widgets and restored references select only available active skills", () => {
  const widget = composerSkillMentionWidget(skills[0]);
  const reference = { kind: "skill" as const, id: "settings", syntax: "@Settings" };
  assert.deepEqual(confirmedComposerSkillIds("@Settings", [widget], [reference], skills), ["settings"]);
  assert.deepEqual(confirmedComposerSkillIds("removed", [widget], [reference], skills), []);
  assert.deepEqual(confirmedComposerSkillIds("@Settings", [widget], [reference], []), []);
  assert.deepEqual(confirmedComposerSkillIds("\\@Settings", [widget], [reference], skills), []);
  assert.deepEqual(confirmedComposerSkillIds("@SettingsLonger", [widget], [reference], skills), []);
});

test("disabled semantic widgets do not select while explicit direct skill widgets remain supported", () => {
  assert.deepEqual(confirmedComposerSkillIds("@Settings", [{ ...composerSkillMentionWidget(skills[0]), enabled: false }], [], skills), []);
  assert.deepEqual(confirmedComposerSkillIds("hello", [{ id: "direct", type: "skill", label: "Settings", sourceItemId: "settings" }], [], skills), ["settings"]);
  assert.deepEqual(confirmedComposerSkillIds("hello", [{ id: "missing", type: "skill", label: "Missing" }], [], skills), []);
});
