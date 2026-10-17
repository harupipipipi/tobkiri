import test from "node:test";
import assert from "node:assert/strict";
import { formatUsage, parseEventText } from "../local.harness-ui/ui/accounting.mjs";

test("single and aggregate usage retain actual provider counts", () => {
  assert.match(formatUsage({ total_tokens: 30, cost_usd: "0.0001" }, 21, 9), /入力 21 \/ 出力 9 \/ 合計 30/);
  assert.match(formatUsage({ tokens: { input_tokens: 1000, output_tokens: 40 } }, 20, 3), /入力 1,000 \/ 出力 40/);
});
test("unknown values are distinct from zero and incomplete cost", () => {
  assert.match(formatUsage({}), /入力 不明/);
  assert.match(formatUsage({ cost_usd: "0" }, 0, 0), /入力 0/);
  assert.match(formatUsage({ cost_known: false, cost_usd: "2" }), /費用 不明$/);
  assert.match(formatUsage({ tokens_complete: false, tokens: {input_tokens: 20} }), /確認済み分・未確定の使用量あり/);
});
test("malformed metadata cannot become an object or executable content", () => {
  for (const text of [null, "null", "[]", "broken", '"x"']) assert.deepEqual(parseEventText(text), {});
  assert.deepEqual(parseEventText('{"run_id":"id"}'), { run_id: "id" });
});
