import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import test from "node:test";
import vm from "node:vm";
import { MessageChannel } from "node:worker_threads";

const source = readFileSync(new URL("./ui/app.js", import.meta.url), "utf8");

test("assembled Harness UI uses SDK bytes and preserves the pinned self-contained Pack", () => {
  const repo = fileURLToPath(new URL("../..", import.meta.url));
  const output = mkdtempSync(join(tmpdir(), "harness-ui-"));
  try {
    execFileSync("python3", ["-B", "-c", `
import importlib.util, pathlib, sys
spec = importlib.util.spec_from_file_location("pack_build", pathlib.Path(sys.argv[1]) / "packs/build.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
module.ui_pack(pathlib.Path(sys.argv[2]))
`, repo, output]);
    const manifest = JSON.parse(readFileSync(join(output, "pack.json")));
    const checkedIn = JSON.parse(readFileSync(join(repo, "packs/local.harness-ui/pack.json")));
    assert.deepEqual(manifest, checkedIn);
    for (const entry of manifest.files) {
      const bytes = readFileSync(join(output, entry.path));
      assert.equal(bytes.length, entry.size);
      assert.equal(`sha256:${createHash("sha256").update(bytes).digest("hex")}`, entry.sha256);
      assert.deepEqual(bytes, readFileSync(join(repo, "packs/local.harness-ui", entry.path)));
    }
    for (const [asset, sdk] of [["ports.js", "index.js"], ["float.js", "float.js"]]) {
      assert.deepEqual(readFileSync(join(output, "ui", asset)), readFileSync(join(repo, "sdk/js/pack-ui", sdk)));
    }
    const profile = JSON.parse(readFileSync(join(repo, "packs/profiles/harness-ui.profile.json")));
    const ordered = (value) => Array.isArray(value) ? value.map(ordered)
      : value !== null && typeof value === "object"
        ? Object.fromEntries(Object.keys(value).sort().map((key) => [key, ordered(value[key])])) : value;
    const hash = `sha256:${createHash("sha256").update(JSON.stringify(ordered(manifest))).digest("hex")}`;
    assert.equal(profile.packs.find((pack) => pack.id === manifest.id).sha256, hash);
  } finally {
    rmSync(output, { recursive: true, force: true });
  }
});

const element = () => ({
  dataset: {},
  handlers: {},
  children: [],
  addEventListener(type, handler) { this.handlers[type] = handler; },
  append(...children) {
    for (const child of children) {
      child.parent = this;
      this.children.push(child);
    }
  },
  remove() { this.parent.children.splice(this.parent.children.indexOf(this), 1); },
});

async function open(window, timers = { setTimeout, clearTimeout }) {
  const elements = Object.fromEntries(
    ["status", "messages", "composer", "text", "send", "stop"].map((name) => [name, element()]),
  );
  elements.send.disabled = true;
  elements.stop.disabled = true;
  const context = vm.createContext({
    window,
    ...timers,
    document: {
      querySelector: (selector) => elements[selector.slice(1)],
      createElement: element,
    },
  });
  const app = new vm.SourceTextModule(source, { context });
  await app.link((specifier, importer) => {
    assert.ok(["./ports.js", "./float.js"].includes(specifier), importer.identifier);
    const url = new URL(specifier, new URL("./ui/", import.meta.url));
    return new vm.SourceTextModule(readFileSync(url, "utf8"), { context });
  });
  await app.evaluate();
  return elements;
}

const windowWithParent = () => Object.assign(new EventTarget(), { parent: {} });

const connect = async (testContext, receive) => {
  const window = windowWithParent();
  const ui = await open(window);
  const { port1, port2 } = new MessageChannel();
  testContext.after(() => {
    window.dispatchEvent(new Event("pagehide"));
    port1.close();
    port2.close();
  });
  port1.onmessage = ({ data }) => receive(data, (result, error) => {
    port1.postMessage({ id: data.id, result, error });
  });
  window.dispatchEvent(Object.assign(new Event("message"), {
    source: window.parent,
    data: { type: "tobkiri-pack-channel" },
    ports: [port2],
  }));
  await Promise.resolve();
  assert.equal(ui.send.disabled, false);
  return ui;
};

const value = (messages) => ({ kind: "value", value: { conversation: "e2e", messages } });
const terminal = (kind) => ({ kind: { kind } });
const output = (kind, text) => ({ kind: {
  kind: "emitted", shape: "local.harness-chat.assistant-output@1.0", value: { kind, text },
} });
const submit = (ui) => {
  ui.text.value = "draft";
  return ui.composer.handlers.submit({ preventDefault() {} });
};

test("the disconnected Harness explains why sending is disabled", async () => {
  let expire;
  const ui = await open(windowWithParent(), {
    setTimeout(callback, delay) {
      assert.equal(delay, 10000);
      expire = callback;
      return 1;
    },
    clearTimeout() {},
  });
  expire();
  await Promise.resolve();
  await Promise.resolve();
  assert.equal(ui.send.disabled, true);
  assert.match(ui.status.textContent, /接続できませんでした/);
});

test("cancelling preserves one draft and waits for the stop request", async (t) => {
  const pulled = Promise.withResolvers();
  const cancelling = Promise.withResolvers();
  const calls = [];
  const ui = await connect(t, (request, respond) => {
    calls.push(request.op ?? request.type);
    if (request.op === "load") respond(value([]));
    if (request.op === "submit") respond({ kind: "stream", handle: 1 });
    if (request.type === "stream-pull") pulled.resolve(respond);
    if (request.type === "stream-cancel") cancelling.resolve(respond);
  });
  const send = submit(ui);
  const replyToPull = await pulled.promise;
  assert.equal(ui.text.value, "draft");
  ui.stop.handlers.click();
  const replyToCancel = await cancelling.promise;
  replyToPull(undefined, { message: "Core refused the UI request" });
  assert.equal(ui.send.disabled, true);
  assert.match(ui.status.textContent, /停止しています/);
  replyToCancel(null);
  await send;
  assert.deepEqual(calls, ["load", "submit", "stream-pull", "stream-cancel"]);
  assert.equal(ui.text.value, "draft");
  assert.deepEqual(ui.messages.children, []);
  assert.equal(ui.send.disabled, false);
  assert.match(ui.status.textContent, /停止しました/);
});

test("failed and cancelled frames restore the draft with different notices", async (t) => {
  const scenarios = [
    ["failed", /会話を完了できませんでした/],
    ["cancelled", /停止しました/],
  ];
  for (const [kind, notice] of scenarios) {
    await t.test(kind, async (subtest) => {
      const ui = await connect(subtest, (request, respond) => {
        if (request.op === "load") respond(value([]));
        if (request.op === "submit") respond({ kind: "stream", handle: 1 });
        if (request.type === "stream-pull") respond([output("delta", "partial"), terminal(kind)]);
        if (request.type === "stream-cancel") respond(null);
      });
      await submit(ui);
      assert.match(ui.status.textContent, notice);
      assert.equal(ui.text.value, "draft");
      assert.deepEqual(ui.messages.children, []);
    });
  }
});

test("an edited draft survives while the failed turn is removed", async (t) => {
  const pulled = Promise.withResolvers();
  const ui = await connect(t, (request, respond) => {
    if (request.op === "load") respond(value([]));
    if (request.op === "submit") respond({ kind: "stream", handle: 1 });
    if (request.type === "stream-pull") pulled.resolve(respond);
    if (request.type === "stream-cancel") respond(null);
  });
  const send = submit(ui);
  const replyToPull = await pulled.promise;
  ui.text.value = "edited draft";
  replyToPull([terminal("failed")]);
  await send;
  assert.match(ui.status.textContent, /会話を完了できませんでした/);
  assert.equal(ui.text.value, "edited draft");
  assert.deepEqual(ui.messages.children, []);
});

test("provider diagnostics survive a failed turn without saving messages or losing the draft", async (t) => {
  let loads = 0;
  const diagnostic = "provider HTTP 400: message: Model <missing-model> was not found";
  const ui = await connect(t, (request, respond) => {
    if (request.op === "load") {
      loads++;
      respond(value([]));
    }
    if (request.op === "submit") respond({ kind: "stream", handle: 1 });
    if (request.type === "stream-pull") respond([output("error", diagnostic), terminal("failed")]);
    if (request.type === "stream-cancel") respond(null);
  });
  await submit(ui);
  assert.equal(loads, 1);
  assert.match(ui.status.textContent, /会話を完了できませんでした/);
  assert.ok(ui.status.textContent.endsWith(diagnostic));
  assert.equal(ui.text.value, "draft");
  assert.deepEqual(ui.messages.children, []);
  assert.equal(ui.send.disabled, false);
});

test("a failed stop request leaves the outcome unconfirmed", async (t) => {
  const pulled = Promise.withResolvers();
  const ui = await connect(t, (request, respond) => {
    if (request.op === "load") respond(value([]));
    if (request.op === "submit") respond({ kind: "stream", handle: 1 });
    if (request.type === "stream-pull") pulled.resolve(respond);
    if (request.type === "stream-cancel") respond(undefined, { message: "Core refused the UI request" });
  });
  const send = submit(ui);
  const replyToPull = await pulled.promise;
  ui.stop.handlers.click();
  replyToPull(undefined, { message: "Core refused the UI request" });
  await send;
  assert.match(ui.status.textContent, /停止を確認できませんでした/);
  assert.equal(ui.text.value, "draft");
  assert.deepEqual(ui.messages.children, []);
});

test("completion racing with a stop checks storage before marking the turn saved", async (t) => {
  const pulled = Promise.withResolvers();
  const verifying = Promise.withResolvers();
  let loads = 0;
  const ui = await connect(t, (request, respond) => {
    if (request.op === "load" && ++loads === 1) respond(value([]));
    else if (request.op === "load") verifying.resolve(respond);
    if (request.op === "submit") respond({ kind: "stream", handle: 1 });
    if (request.type === "stream-pull") pulled.resolve(respond);
    if (request.type === "stream-cancel") respond(undefined, { message: "stream already completed" });
  });
  const send = submit(ui);
  const replyToPull = await pulled.promise;
  ui.stop.handlers.click();
  replyToPull([output("delta", "answer"), output("done"), terminal("completed")]);
  const replyToLoad = await verifying.promise;
  assert.deepEqual(ui.messages.children.map((row) => row.children[1].textContent), ["◷ 保存待ち", "保存を確認中"]);
  replyToLoad(value([{ role: "user", content: "draft" }, { role: "assistant", content: "answer" }]));
  await send;
  assert.equal(ui.text.value, "");
  assert.deepEqual(ui.messages.children.map((row) => row.children[1].textContent), ["✓ 保存済み", "✓ 保存済み"]);
  assert.match(ui.status.textContent, /準備できました/);
});

test("completion alone does not mark an unverified turn saved", async (t) => {
  const oldTurn = [{ role: "user", content: "draft" }, { role: "assistant", content: "answer" }];
  const ui = await connect(t, (request, respond) => {
    if (request.op === "load") respond(value(oldTurn));
    if (request.op === "submit") respond({ kind: "stream", handle: 1 });
    if (request.type === "stream-pull") respond([output("delta", "answer"), terminal("completed")]);
    if (request.type === "stream-cancel") respond(null);
  });
  await submit(ui);
  assert.match(ui.status.textContent, /保存を確認できませんでした/);
  assert.equal(ui.text.value, "draft");
  assert.deepEqual(ui.messages.children, []);
});
