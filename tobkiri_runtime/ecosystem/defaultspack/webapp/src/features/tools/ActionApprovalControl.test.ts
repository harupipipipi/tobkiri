import assert from "node:assert/strict";
import test from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { ActionApprovalControl, ACTION_APPROVAL_OPTIONS } from "./ActionApprovalControl";

test("approval menu offers only directly selectable approval modes", () => {
  assert.deepEqual(
    ACTION_APPROVAL_OPTIONS.map((option) => option.mode),
    ["ask", "agent", "full"],
  );
  assert.equal(
    ACTION_APPROVAL_OPTIONS.some((option) => option.label === "カスタム（設定）"),
    false,
  );
});


test("unsupported approval selector explains the policy accessibly without showing active full mode", () => {
  const reason = "この会話の承認は設定された権限に従います。ここで代理承認やフルアクセスに変更する機能は未対応です。";
  const html = renderToStaticMarkup(createElement(ActionApprovalControl, {
    mode: "ask", disabled: true, disabledReason: reason, surfaceClassName: "",
    onModeChange: () => assert.fail("unsupported policy must not change"),
  }));
  assert.match(html, /disabled=""/);
  assert.ok(html.includes(`aria-description="${reason}"`));
  assert.ok(html.includes(`title="${reason}"`));
  assert.match(html, />ポリシー</);
  assert.doesNotMatch(html, /role="menuitemradio"/);
});
