import assert from "node:assert/strict";
import test from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { ApprovalPreferenceSettings } from "./ApprovalPreferenceSettings";

test("default settings show visibility checkbox and mandatory fixed ask selection", () => {
  const html = renderToStaticMarkup(createElement(ApprovalPreferenceSettings, { tools: {}, onSettingChange: () => assert.fail("render must not write defaults") }));
  assert.match(html, /type="checkbox" checked=""/);
  assert.match(html, /required=""/);
  assert.match(html, /value="ask" selected=""/);
  assert.match(html, /value="agent" disabled=""/);
  assert.match(html, /value="full" disabled=""/);
});

test("unsupported saved fixed preference remains visible with explanation", () => {
  const html = renderToStaticMarkup(createElement(ApprovalPreferenceSettings, { tools: { show_action_approval_control: false, fixed_action_approval_mode: "full" }, onSettingChange: () => assert.fail("render must not replace saved preference") }));
  assert.match(html, /value="full" disabled="" selected=""/);
  assert.match(html, /role="status"/);
});


test("unsupported visible selection offers explicit recovery without overwriting during render", () => {
  const html = renderToStaticMarkup(createElement(ApprovalPreferenceSettings, {
    tools: { action_approval_mode: "full" },
    onSettingChange: () => assert.fail("render must not silently downgrade a saved mode"),
  }));
  assert.match(html, /入力欄のモードを「人が承認」に戻す/);
  assert.match(html, /保存された入力欄の承認モードは、現在利用できません/);
  const hidden = renderToStaticMarkup(createElement(ApprovalPreferenceSettings, {
    tools: { show_action_approval_control: false, action_approval_mode: "full", fixed_action_approval_mode: "ask" },
    onSettingChange: () => assert.fail("render must preserve the inactive selected preference"),
  }));
  assert.doesNotMatch(hidden, /入力欄のモードを「人が承認」に戻す/);
});
