import test from "node:test";
import assert from "node:assert/strict";
import { ConversationMutationBarrier } from "./conversationMutationBarrier";

test("rapid model selection settles before first authoritative revision read and one start", async () => {
  const barrier = new ConversationMutationBarrier();
  const operations: string[] = [];
  let resolve!: () => void;
  const write = new Promise<void>((done) => { resolve = done; }).then(() => { operations.push("model-write"); });
  barrier.record("store-a", "chat-1", write);
  const send = (async () => {
    await barrier.wait("store-a", "chat-1");
    operations.push("revision-read", "start");
  })();
  await Promise.resolve();
  assert.deepEqual(operations, []);
  resolve(); await send;
  assert.deepEqual(operations, ["model-write", "revision-read", "start"]);
});

test("failed model update prevents dispatch, and writes are store/conversation scoped", async () => {
  const barrier = new ConversationMutationBarrier();
  barrier.record("store-a", "chat-1", Promise.reject(new Error("model write refused")));
  await barrier.wait("store-b", "chat-1");
  await barrier.wait("store-a", "chat-2");
  let starts = 0;
  await assert.rejects(async () => { await barrier.wait("store-a", "chat-1"); starts += 1; }, /model write refused/);
  assert.equal(starts, 0);
});

test("superseded older write must settle even when the newer selection fails first", async () => {
  const barrier = new ConversationMutationBarrier();
  let releaseOld!: () => void;
  const old = new Promise<void>((resolve) => { releaseOld = resolve; });
  barrier.record("store-a", "chat", old);
  barrier.record("store-a", "chat", Promise.reject(new Error("new selection failed")));
  let settled = false;
  const waiting = barrier.wait("store-a", "chat").finally(() => { settled = true; });
  void waiting.catch(() => undefined);
  await Promise.resolve(); await Promise.resolve();
  assert.equal(settled, false);
  releaseOld();
  await assert.rejects(waiting, /new selection failed/);
  await barrier.wait("store-a", "chat");
});

test("writes recorded during a barrier are also drained before the revision read", async () => {
  const barrier = new ConversationMutationBarrier();
  let releaseFirst!: () => void;
  let releaseSecond!: () => void;
  barrier.record("store-a", "chat", new Promise<void>((resolve) => { releaseFirst = resolve; }));
  let ready = false;
  const waiting = barrier.wait("store-a", "chat").then(() => { ready = true; });
  barrier.record("store-a", "chat", new Promise<void>((resolve) => { releaseSecond = resolve; }));
  releaseFirst(); await Promise.resolve(); await Promise.resolve();
  assert.equal(ready, false);
  releaseSecond(); await waiting;
  assert.equal(ready, true);
});
