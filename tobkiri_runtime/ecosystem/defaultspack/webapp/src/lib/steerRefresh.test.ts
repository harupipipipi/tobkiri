import test from "node:test";
import assert from "node:assert/strict";

import {
  passiveSteerRefreshStatus,
  SteerRefreshFence,
  steerRefreshContextKey,
} from "./steerRefresh";

function deferred<T>(): {
  promise: Promise<T>;
  resolve: (value: T) => void;
} {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((complete) => { resolve = complete; });
  return { promise, resolve };
}

test("passive missing saved-turn reads stay pending rather than claiming a guidance failure", () => {
  const status = passiveSteerRefreshStatus("unsettled");

  assert.equal(status.kind, "pending");
  assert.equal(status.message, "送信状況を確認しています。");
  assert.doesNotMatch(status.message, /失敗/);
  assert.doesNotMatch(status.message, /追加指示/);
});

test("passive unavailable reads stay pending rather than claiming a guidance failure", () => {
  const status = passiveSteerRefreshStatus("unavailable");

  assert.equal(status.kind, "pending");
  assert.equal(status.message, "接続を待っています。送信結果はまだ確認できていません。");
  assert.doesNotMatch(status.message, /失敗/);
  assert.doesNotMatch(status.message, /追加指示/);
});

test("late saved-turn reads cannot restore busy or status after settlement, a switch, or ABA", async () => {
  const fence = new SteerRefreshFence();
  const firstContext = steerRefreshContextKey("conversation-1", "turn-1");
  const secondContext = steerRefreshContextKey("conversation-2", "turn-2");
  assert.ok(firstContext);
  assert.ok(secondContext);

  const settledRead = deferred<void>();
  fence.synchronize(firstContext);
  const settledToken = fence.begin(firstContext);
  assert.ok(settledToken);
  const applied: string[] = [];
  const applySettledRead = settledRead.promise.then(() => {
    if (settledToken && fence.isCurrent(settledToken)) applied.push("settled-status-and-busy");
  });
  fence.synchronize(null);
  settledRead.resolve();
  await applySettledRead;

  const switchedRead = deferred<void>();
  fence.synchronize(firstContext);
  const staleQueueTicket = fence.capture(firstContext);
  assert.ok(staleQueueTicket);
  const switchedToken = fence.begin(firstContext);
  assert.ok(switchedToken);
  const applySwitchedRead = switchedRead.promise.then(() => {
    if (switchedToken && fence.isCurrent(switchedToken)) applied.push("switched-status-and-busy");
  });
  fence.synchronize(secondContext);
  fence.synchronize(firstContext);
  switchedRead.resolve();
  await applySwitchedRead;

  assert.deepEqual(applied, []);
  assert.equal(fence.matches(staleQueueTicket), false);

  const currentRead = deferred<void>();
  const currentToken = fence.begin(firstContext);
  assert.ok(currentToken);
  const currentApplied: string[] = [];
  const applyCurrentRead = currentRead.promise.then(() => {
    if (currentToken && fence.isCurrent(currentToken)) currentApplied.push("current-status-and-busy");
  });
  currentRead.resolve();
  await applyCurrentRead;

  assert.deepEqual(currentApplied, ["current-status-and-busy"]);
  assert.equal(fence.isCurrent(currentToken), true);
  assert.equal(fence.matches(fence.capture(firstContext)), true);
});
