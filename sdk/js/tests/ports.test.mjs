import test from "node:test";
import assert from "node:assert/strict";
import { connectPackUi, PackPortClient } from "../pack-ui/index.js";

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

test("stream pulls allow a slow first frame and respect an explicit shorter transport timeout", async () => {
  const port = new Port();
  const client = new PackPortClient(port);
  const opened = client.call("local.demo.stream@1.0", "open", {});
  port.reply(1, { kind: "stream", handle: 9 });
  const stream = await opened;
  const original = globalThis.setTimeout;
  const delays = [];
  globalThis.setTimeout = (callback, ms) => {
    delays.push(ms);
    return original(callback, ms);
  };
  try {
    const first = stream.pull(1);
    port.reply(2, [{ kind: "first-frame" }]);
    assert.equal((await first)[0].kind, "first-frame");
    const second = stream.pull(1, { timeoutMs: 1234 });
    port.reply(3, []);
    assert.deepEqual(await second, []);
    assert.deepEqual(delays, [300000, 1234]);
  } finally {
    globalThis.setTimeout = original;
    client.close();
  }
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

test("UI connection accepts only the expected parent and cleans up listeners", async () => {
  const previousWindow = globalThis.window;
  const listeners = new Map();
  const parent = {};
  globalThis.window = {
    parent,
    addEventListener(type, listener) { listeners.set(type, listener); },
    removeEventListener(type, listener) { if (listeners.get(type) === listener) listeners.delete(type); },
  };
  try {
    const connection = connectPackUi(parent, { timeoutMs: 1000 });
    const port = new Port();
    listeners.get("message")({ source: {}, data: { type: "tobkiri-pack-channel" }, ports: [port] });
    assert.equal(listeners.has("message"), true);
    listeners.get("message")({ source: parent, data: { type: "tobkiri-pack-channel" }, ports: [port] });
    const client = await connection;
    assert.equal(listeners.has("message"), false);
    listeners.get("pagehide")();
    assert.equal(port.closed, true);
    client.close();
  } finally { globalThis.window = previousWindow; }
});

test("UI connection times out and removes its listeners", async () => {
  const previousWindow = globalThis.window;
  const listeners = new Map();
  globalThis.window = {
    parent: {},
    addEventListener(type, listener) { listeners.set(type, listener); },
    removeEventListener(type, listener) { if (listeners.get(type) === listener) listeners.delete(type); },
  };
  try {
    await assert.rejects(connectPackUi(window.parent, { timeoutMs: 1 }), /connection timed out/);
    assert.equal(listeners.size, 0);
  } finally { globalThis.window = previousWindow; }
});

test("leaving the page cancels an outstanding UI connection", async () => {
  const previousWindow = globalThis.window;
  const listeners = new Map();
  globalThis.window = {
    parent: {},
    addEventListener(type, listener) { listeners.set(type, listener); },
    removeEventListener(type, listener) { if (listeners.get(type) === listener) listeners.delete(type); },
  };
  try {
    const connection = connectPackUi(window.parent, { timeoutMs: 1000 });
    listeners.get("pagehide")();
    await assert.rejects(connection, /connection cancelled/);
    assert.equal(listeners.size, 0);
  } finally { globalThis.window = previousWindow; }
});
