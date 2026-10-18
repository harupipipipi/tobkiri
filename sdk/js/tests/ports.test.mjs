import test from "node:test";
import assert from "node:assert/strict";
import { PackPortClient } from "../pack-ui/index.js";

class Port {
  messages = [];
  start() {}
  close() { this.closed = true; }
  postMessage(value) { this.messages.push(value); }
  reply(id, result, error) { this.onmessage({ data: { id, result, error } }); }
}

test("UI call unwraps a canonical value without supplying identity", async () => {
  const port = new Port();
  const client = new PackPortClient(port);
  const pending = client.call("local.demo.value@1.0", "read", {});
  assert.deepEqual(port.messages[0], { type: "call", id: 1, shape: "local.demo.value@1.0", op: "read", args: {} });
  port.reply(1, { kind: "value", value: { float: "1e-1" } });
  assert.deepEqual(await pending, { float: "1e-1" });
  client.close();
  assert.deepEqual(port.messages.at(-1), { type: "close" });
});

test("typed UI refusals retain only closed codes and the fixed host message", async () => {
  for (const code of [
    "approval-required", "blocked", "cancelled", "closed", "conflict",
    "consumed", "deadline", "denied", "duplicate", "internal", "invalid",
    "limit", "limit-exceeded", "not-found", "persistence", "registration-full",
    "stale-epoch", "trap", "unavailable",
  ]) {
    const port = new Port();
    const client = new PackPortClient(port);
    const pending = client.call("local.demo.value@1.0", "read", {});
    port.reply(1, undefined, { code, message: "Core refused the UI request", payload: "private-payload", stack: "private-stack" });
    await assert.rejects(pending, (error) => {
      assert.ok(error instanceof Error);
      assert.equal(error.code, code);
      assert.equal(error.message, "Core refused the UI request");
      assert.deepEqual(Object.keys(error), ["code"]);
      assert.equal(Object.getOwnPropertyDescriptor(error, "code").writable, false);
      assert.equal(error.payload, undefined);
      assert.equal(error.stack.includes("private-stack"), false);
      return true;
    });
    assert.equal(port.messages.length, 1, "a refusal cannot initiate another request");
    client.close();
  }
});

test("malformed or unknown host codes and private error fields are not copied", async () => {
  const hostile = { toString() { throw new Error("must not coerce a code"); } };
  const errors = [
    ...[undefined, null, 0, false, hostile, [], "NOT_FOUND", "not_found", "not-found\n", "x".repeat(65), "provider-secret", "__proto__", "constructor"]
      .map((code) => ({ code, message: "private-body", stack: "private-stack", payload: { secret: "private-payload" } })),
    Object.create({ code: "not-found", message: "private-body" }),
    Object.assign([], { code: "not-found", message: "private-body" }),
  ];
  for (const value of errors) {
    const port = new Port();
    const client = new PackPortClient(port);
    const pending = client.call("local.demo.value@1.0", "read", {});
    port.reply(1, undefined, value);
    await assert.rejects(pending, (error) => {
      assert.equal(Object.hasOwn(error, "code"), false);
      assert.equal(error.message, "Port call failed");
      assert.equal(error.stack.includes("private-"), false);
      assert.equal(error.payload, undefined);
      return true;
    });
    client.close();
  }
});

test("only the existing fixed public host messages survive refusal decoding", async () => {
  for (const message of ["Core refused the UI request", "Invalid UI request", "UI request refused", "Core response exceeded the UI limit"]) {
    const port = new Port();
    const client = new PackPortClient(port);
    const pending = client.call("local.demo.value@1.0", "read", {});
    port.reply(1, undefined, { code: "invalid", message });
    await assert.rejects(pending, (error) => error.code === "invalid" && error.message === message);
    client.close();
  }
});

test("typed stream failure preserves its code through the existing cancellation path", async () => {
  const port = new Port();
  const client = new PackPortClient(port);
  const opened = client.call("local.demo.stream@1.0", "open", {});
  port.reply(1, { kind: "stream", handle: 7 });
  const stream = await opened;
  const pulled = stream.pull(1);
  port.reply(2, undefined, { code: "conflict", message: "Core refused the UI request" });
  await assert.rejects(pulled, (error) => error.code === "conflict");
  assert.deepEqual(port.messages.at(-1), { type: "stream-cancel", id: 3, handle: 7 });
  port.reply(3, null);
  client.close();
});

test("opaque UI stream pulls are bounded and cancellation is explicit", async () => {
  const port = new Port();
  const client = new PackPortClient(port);
  const pending = client.call("local.demo.stream@1.0", "open", {});
  port.reply(1, { kind: "stream", handle: 9 });
  const stream = await pending;
  await assert.rejects(stream.pull(33), /1\.\.32/);
  await assert.rejects(stream.pull(0), /1\.\.32/);
  const pulled = stream.pull(2);
  assert.deepEqual(port.messages.at(-1), { type: "stream-pull", id: 2, handle: 9, max: 2 });
  port.reply(2, [{ kind: { kind: "emitted", value: { text: "猫" } } }]);
  assert.equal((await pulled)[0].kind.value.text, "猫");
  const cancelled = stream.cancel();
  assert.deepEqual(port.messages.at(-1), { type: "stream-cancel", id: 3, handle: 9 });
  port.reply(3, null);
  await cancelled;
  await assert.rejects(stream.pull(1), /closed/);
  client.close();
});

test("closing the UI cancels pending calls and invalidates stream views", async () => {
  const port = new Port();
  const client = new PackPortClient(port);
  const opened = client.call("local.demo.stream@1.0", "open", {});
  port.reply(1, { kind: "stream", handle: 7 });
  const stream = await opened;
  const pulled = stream.pull(1);
  client.close();
  await assert.rejects(pulled, /session closed/);
  await assert.rejects(stream.pull(1), /closed/);
  assert.equal(port.closed, true);
});

test("host channel retirement rejects every pending call and future request", async () => {
  const port = new Port();
  const client = new PackPortClient(port);
  const one = client.call("local.demo.value@1.0", "read", {});
  const two = client.call("local.demo.value@1.0", "read", {});
  port.onmessage({ data: { type: "tobkiri-pack-closed" } });
  await assert.rejects(one, /session closed/);
  await assert.rejects(two, /session closed/);
  await assert.rejects(client.call("local.demo.value@1.0", "read", {}), /session closed/);
  assert.equal(port.closed, true);
});

test("AbortSignal cancels the existing request id without any authority field", async () => {
  const port = new Port();
  const client = new PackPortClient(port);
  const controller = new AbortController();
  const pending = client.call("local.demo.value@1.0", "read", {}, { signal: controller.signal });
  controller.abort();
  await assert.rejects(pending, /cancelled/);
  assert.deepEqual(port.messages.at(-1), { type: "cancel", id: 1 });
  client.close();
});

test("a failed send preserves the next host request sequence", async () => {
  const port = new Port();
  const send = port.postMessage.bind(port);
  let fail = true;
  port.postMessage = (message) => {
    if (fail) { fail = false; throw new Error("clone failed"); }
    send(message);
  };
  const client = new PackPortClient(port);
  await assert.rejects(client.call("local.demo.value@1.0", "read", {}), /clone failed/);
  const pending = client.call("local.demo.value@1.0", "read", {});
  assert.equal(port.messages.at(-1).id, 1);
  port.reply(1, { kind: "value", value: "ok" });
  assert.equal(await pending, "ok");
  client.close();
});

test("a failed cancellation send still rejects the pending caller", async () => {
  const port = new Port();
  const client = new PackPortClient(port);
  const controller = new AbortController();
  const pending = client.call("local.demo.value@1.0", "read", {}, { signal: controller.signal });
  port.postMessage = () => { throw new Error("port unavailable"); };
  controller.abort();
  await assert.rejects(pending, /cancelled/);
  client.close();
});
