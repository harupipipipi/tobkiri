import assert from "node:assert/strict";
import test from "node:test";

import { ACTION_APPROVAL_OPTIONS } from "./ActionApprovalControl";

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
