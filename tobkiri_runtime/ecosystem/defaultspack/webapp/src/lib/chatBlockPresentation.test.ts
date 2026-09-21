import test from "node:test";
import assert from "node:assert/strict";

import { CHAT_BLOCK_PRESENTATION_SCHEMA, isPublicChatBlock, safeUnknownBlockDetails, unknownBlockDiagnostic } from "./chatBlockPresentation";

test("public presentation schema is versioned and allowlisted", () => {
  assert.equal(CHAT_BLOCK_PRESENTATION_SCHEMA, "rumi.chat.public.v1");
  assert.equal(isPublicChatBlock({ type: "markdown", text: "safe" }), true);
  assert.equal(isPublicChatBlock({ type: "future.v2", text: "not implicitly public" }), false);
  assert.equal(isPublicChatBlock(null), false);
});

test("unknown diagnostics omit secrets, reasoning, paths, args, results, and payload values", () => {
  const block = {
    type: "provider.future",
    schema_version: "v9",
    token: "do-not-leak",
    tool_arguments: { password: "do-not-leak" },
    tool_result: "do-not-leak",
    hidden_reasoning: "do-not-leak",
    private_path: "/Users/private/file",
    binary_payload: "A".repeat(100_000),
    status: "do-not-copy-value",
  };
  const details = safeUnknownBlockDetails(block);
  assert.doesNotMatch(details, /do-not-leak|do-not-copy-value|Users|AAAA/);
  assert.doesNotMatch(details, /token|argument|result|reasoning|path|payload/i);
  assert.deepEqual(unknownBlockDiagnostic(block).publicFieldNames, ["status"]);
});

test("malformed and huge unknown values produce bounded diagnostics", () => {
  assert.equal(unknownBlockDiagnostic("malformed").blockType, "malformed");
  const details = safeUnknownBlockDetails({ type: "future", value: "x".repeat(1_000_000) });
  assert.ok(details.length < 1000);
  assert.doesNotMatch(details, /xxxx/);
});

test("unknown type values are not reflected in developer diagnostics", () => {
  const details = safeUnknownBlockDetails({ type: "secret-do-not-leak", value: "ignored" });
  assert.match(details, /"blockType": "unknown"/);
  assert.doesNotMatch(details, /secret-do-not-leak/);
});
