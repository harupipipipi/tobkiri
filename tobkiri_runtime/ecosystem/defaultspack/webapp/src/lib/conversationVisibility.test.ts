import assert from "node:assert/strict";
import test from "node:test";
import { conversationVisibleInHistory } from "./conversationVisibility";

test("only an explicit owner hidden flag omits a record from main history", () => {
  assert.equal(conversationVisibleInHistory({ metadata: { is_hidden: true } }), false);
  for (const metadata of [null, undefined, {}, { is_hidden: false }, { is_hidden: "true" }, { conversation_channel: "side" }, Object.create({ is_hidden: true })]) {
    assert.equal(conversationVisibleInHistory({ metadata }), true);
  }
  const source = { metadata: { is_hidden: true, parent_conversation_id: "parent" } };
  conversationVisibleInHistory(source);
  assert.deepEqual(source.metadata, { is_hidden: true, parent_conversation_id: "parent" });
});
