import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import test from "node:test";
import vm from "node:vm";

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
