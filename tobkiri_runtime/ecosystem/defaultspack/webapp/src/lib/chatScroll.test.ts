import test from "node:test";
import assert from "node:assert/strict";

import { isMessageScrollerNearBottom } from "./chatScroll";

test("message scroller follows updates while it remains near the bottom", () => {
  assert.equal(isMessageScrollerNearBottom({
    clientHeight: 600,
    scrollHeight: 2_000,
    scrollTop: 1_320,
  }), true);
});

test("message scroller stops following after the user reads older content", () => {
  assert.equal(isMessageScrollerNearBottom({
    clientHeight: 600,
    scrollHeight: 2_000,
    scrollTop: 900,
  }), false);
});

test("message scroller treats an exact threshold distance as near the bottom", () => {
  assert.equal(isMessageScrollerNearBottom({
    clientHeight: 600,
    scrollHeight: 2_000,
    scrollTop: 1_304,
  }), true);
});
