import test from "node:test";
import assert from "node:assert/strict";

import { normalizeLocale, t } from "./i18n";

test("i18n normalizes explicit and auto locales", () => {
  assert.equal(normalizeLocale("en"), "en");
  assert.equal(normalizeLocale("zh-CN"), "zh");
  assert.equal(normalizeLocale("ko-KR"), "ko");
  assert.equal(normalizeLocale("es-MX"), "es");
  assert.equal(normalizeLocale("fr-FR"), "fr");
  assert.equal(normalizeLocale("de-DE"), "de");
  assert.equal(normalizeLocale("auto", "en-US"), "en");
  assert.equal(normalizeLocale("auto", "ja-JP"), "ja");
  assert.equal(normalizeLocale("auto", "zh-CN"), "zh");
});

test("i18n translates frontend and tool namespaces", () => {
  assert.equal(t("ja", "spotlight.placeholder"), "会話履歴を検索");
  assert.equal(t("ja", "spotlight.title"), "会話検索");
  assert.match(t("ja", "spotlight.description"), /Page Down/);
  assert.equal(t("en", "tools.assist.vector"), "Vector: recommend relevant tools");
  assert.equal(t("en", "spotlight.matches", { count: 3 }), "3 matches");
  assert.equal(t("en", "spotlight.resultCount", { count: 12 }), "12 conversations found.");
  assert.equal(t("ja", "promptStudio.tab.selected"), "選択中");
  assert.equal(t("en", "promptStudio.modelBoundary"), "Model selection is used only for this Studio Test context. Prompt text cannot switch models, grant permissions, or execute tools.");
  assert.equal(t("ja", "promptStudio.verdict.toolSchemaNoSelectedTool"), "この Studio テストには選択ツールが指定されていません。");
  assert.equal(t("es", "promptStudio.verdict.safetyTitle"), "Límite de seguridad");
  assert.equal(t("fr", "promptStudio.promptToolBoundary"), "Le texte du prompt peut suggérer une pertinence, mais ne peut pas attacher ni exécuter des outils.");
  assert.equal(t("de", "tools.assist.off"), "Aus: nur manuell ausgewählte Tools");
});
