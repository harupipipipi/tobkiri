import assert from "node:assert/strict";
import test from "node:test";

import {
  FALLBACK_SETTINGS_ASSISTANT_SKILL,
  SETTINGS_ASSISTANT_SKILL_ID,
  createSettingsModeDraft,
  isSettingsModeInput,
  normalizeComposerHomeTitle,
  resolveComposerHomeMode,
  resolveComposerHomeTitle,
} from "./settingsMode";

test("settings mode draft uses the ordinary composer skill mention contract", () => {
  const draft = createSettingsModeDraft(FALLBACK_SETTINGS_ASSISTANT_SKILL);

  assert.equal(draft.input, "@Settings ");
  assert.deepEqual(draft.references, [{
    kind: "skill",
    id: SETTINGS_ASSISTANT_SKILL_ID,
    syntax: "@Settings",
  }]);
  assert.equal(draft.widgets[0]?.metadata?.source, "composer_at_mention");
  assert.equal(isSettingsModeInput(draft.input, FALLBACK_SETTINGS_ASSISTANT_SKILL), true);
});

test("settings mode ends as soon as the Settings mention is removed", () => {
  assert.equal(isSettingsModeInput("設定を相談したい", FALLBACK_SETTINGS_ASSISTANT_SKILL), false);
  assert.equal(isSettingsModeInput("@Settings 設定を相談したい", FALLBACK_SETTINGS_ASSISTANT_SKILL), true);
  assert.equal(isSettingsModeInput("Settings 設定を相談したい", FALLBACK_SETTINGS_ASSISTANT_SKILL), false);
});

test("home title resolution deduplicates repeated mentions and falls back cleanly", () => {
  const skills = [FALLBACK_SETTINGS_ASSISTANT_SKILL];

  assert.equal(resolveComposerHomeTitle("@Settings @settings 比較して", skills), "Settings Mode");
  assert.deepEqual(resolveComposerHomeMode("@Settings @settings 比較して", skills), {
    id: "settings",
    priority: 100,
    skillId: SETTINGS_ASSISTANT_SKILL_ID,
    title: "Settings Mode",
  });
  assert.equal(resolveComposerHomeTitle("@Set 比較して", skills), "Tobkiri");
  assert.equal(resolveComposerHomeTitle("比較して", skills), "Tobkiri");
  assert.equal(resolveComposerHomeTitle("比較して", skills, "My Assistant"), "My Assistant");
  assert.equal(resolveComposerHomeTitle("@Settings 比較して", skills, "My Assistant"), "Settings Mode");
});

test("custom home titles normalize whitespace, reset aliases, and length", () => {
  assert.equal(normalizeComposerHomeTitle("  My   Assistant  "), "My Assistant");
  assert.equal(normalizeComposerHomeTitle("reset"), "Tobkiri");
  assert.equal(normalizeComposerHomeTitle("DEFAULT"), "Tobkiri");
  assert.equal(normalizeComposerHomeTitle("x".repeat(80)).length, 48);
});
