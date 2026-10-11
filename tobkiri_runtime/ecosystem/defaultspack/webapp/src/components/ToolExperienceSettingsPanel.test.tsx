import test from "node:test";
import assert from "node:assert/strict";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { ToolExperienceSettingsPanel } from "./ToolExperienceSettingsPanel";

const render = (mode: string) => renderToStaticMarkup(createElement(ToolExperienceSettingsPanel, {
  tools: [], settingsValues: { tools: { default_mode: mode } }, onSettingChange: () => undefined,
}));

test("tool defaults explain lasting scope, overrides, and separate execution approval", () => {
  const html = render("auto");
  assert.match(html, /普段のツールの選び方/);
  assert.match(html, /今後のメッセージ/);
  assert.match(html, /会話の既定値や入力欄のモード指定/);
  assert.match(html, /選ばれたツールが必ず実行されるとは限りません/);
  assert.match(html, /ツール選択の確認と、ファイル変更などの操作の承認は別/);
  assert.doesNotMatch(html, /このメッセージでは外部機能/);
});

test("each unchanged mode value has distinct actor and timing explanations", () => {
  for (const [mode, label, explanation] of [
    ["auto", "Tobkiriが自動で選ぶ", "@候補をEnter・Tab・クリックで確定すると"],
    ["review", "選ばれたツールを確認", "候補を確認してから回答を始めます"],
    ["manual", "@で使うツールを指定", "指定がなければツールを使いません"],
    ["none", "ツールを使わずに回答", "@指定があってもツールは使いません"],
  ]) {
    const html = render(mode);
    assert.match(html, new RegExp(label));
    assert.match(html, new RegExp(explanation));
    const cards = [...html.matchAll(/<button[^>]*>[\s\S]*?<\/button>/g)].map((match) => match[0]);
    const selected = cards.find((card) => card.includes(label));
    assert.ok(selected?.includes("lucide-check"), `${mode} remains the selected setting`);
  }
});


test("Settings offers the initially hidden tool control with practical examples", () => {
  const html = render("auto");
  assert.match(html, /入力欄に「機能の使い方」を表示/);
  assert.match(html, /普段は非表示です/);
  for (const example of ["Webで天気を調べて", "@Web Search", "文章の言い換え", "Enter・Tab・クリック"]) {
    assert.ok(html.includes(example));
  }
});
