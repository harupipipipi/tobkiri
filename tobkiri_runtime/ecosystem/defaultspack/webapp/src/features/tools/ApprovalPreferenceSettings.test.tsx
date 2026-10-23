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

const reviewerModels = [{ profile_id: "registered-reviewer", display_name: "Review AI", model_id: "raw-model-id", qualified_model_id: "provider/raw-model-id" }];

test("reviewer selection saves registered profile IDs, preserves empty and stale preferences", () => {
  const saved = renderToStaticMarkup(createElement(ApprovalPreferenceSettings, { tools: { approval_reviewer_model: "registered-reviewer" }, reviewerModels, onSettingChange: () => assert.fail("render must not save") }));
  assert.match(saved, /value="registered-reviewer" selected=""/);
  assert.doesNotMatch(saved, /value="raw-model-id"|value="provider\/raw-model-id"|type="text"/);
  assert.match(saved, /この設定だけでは代理承認は有効になりません/);
  const empty = renderToStaticMarkup(createElement(ApprovalPreferenceSettings, { tools: {}, reviewerModels, onSettingChange: () => assert.fail("must not choose fallback") }));
  assert.match(empty, /value="" selected=""/);
  assert.match(empty, /未選択（代理承認は利用できません）/);
  const stale = renderToStaticMarkup(createElement(ApprovalPreferenceSettings, { tools: { approval_reviewer_model: "removed-profile" }, reviewerModels, onSettingChange: () => assert.fail("must not replace stale reference") }));
  assert.match(stale, /value="removed-profile" disabled="" selected=""/);
});

test("reviewer change handlers reject arbitrary model values and save only admitted identity", () => {
  const changes: unknown[][] = [];
  const tree = ApprovalPreferenceSettings({ tools: {}, reviewerModels, onSettingChange: (...args) => changes.push(args) });
  type Element = { type?: unknown; props?: { children?: unknown; onChange?: (event: { target: { value: string } }) => void } };
  const elements: Element[] = [];
  const visit = (value: unknown): void => {
    if (Array.isArray(value)) value.forEach(visit);
    else if (value && typeof value === "object") {
      const element = value as Element;
      elements.push(element);
      visit(element.props?.children);
    }
  };
  visit(tree);
  const selects = elements.filter((element) => element.type === "select");
  const reviewer = selects[selects.length - 1];
  assert.ok(reviewer?.props?.onChange);
  for (const value of ["raw-model-id", "provider/raw-model-id", "removed-profile", "registered-reviewer", ""]) reviewer.props.onChange({ target: { value } });
  assert.deepEqual(changes, [["tools", "approval_reviewer_model", "registered-reviewer"], ["tools", "approval_reviewer_model", ""]]);
});
