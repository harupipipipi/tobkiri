import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

const source = readFileSync(new URL("./ui/app.js", import.meta.url), "utf8");

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

async function open(connect) {
  const elements = Object.fromEntries(
    ["status", "messages", "composer", "text", "send", "stop"].map((name) => [name, element()]),
  );
  elements.send.disabled = true;
  const context = vm.createContext({ document: {
    querySelector: (selector) => elements[selector.slice(1)],
    createElement: element,
  } });
  const app = new vm.SourceTextModule(source, { context });
  const ports = new vm.SyntheticModule(["connectPackUi"], function () {
    this.setExport("connectPackUi", connect);
  }, { context });
  await app.link((specifier) => {
    assert.equal(specifier, "./ports.js");
    return ports;
  });
  await app.evaluate();
  return elements;
}

test("the disconnected Harness explains why sending is disabled", async () => {
  const ui = await open(async () => { throw new Error("connection timeout"); });
  assert.equal(ui.send.disabled, true);
  assert.match(ui.status.textContent, /接続できませんでした/);
});

test("cancelling preserves one draft and waits for the stop request", async () => {
  const started = Promise.withResolvers();
  const frame = Promise.withResolvers();
  const stopping = Promise.withResolvers();
  const stream = {
    pull() { started.resolve(); return frame.promise; },
    cancel() { frame.reject(new Error("request cancelled")); return stopping.promise; },
  };
  const calls = [];
  const ui = await open(async () => ({
    async call(_shape, operation) {
      calls.push(operation);
      return operation === "load" ? { messages: [] } : stream;
    },
  }));
  ui.text.value = "draft";
  const send = ui.composer.handlers.submit({ preventDefault() {} });
  await started.promise;
  await ui.composer.handlers.submit({ preventDefault() {} });
  ui.stop.handlers.click();
  assert.equal(ui.send.disabled, true);
  stopping.resolve();
  await send;
  assert.deepEqual(calls, ["load", "submit"]);
  assert.equal(ui.text.value, "draft");
  assert.deepEqual(ui.messages.children, []);
  assert.equal(ui.send.disabled, false);
  assert.match(ui.status.textContent, /停止しました/);
});
