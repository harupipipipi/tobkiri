import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { runInNewContext } from "node:vm";
import { formatUsage, parseEventText } from "../local.deepthink-ui/ui/accounting.mjs";

// Exercise the real UI control flow with an in-memory DOM/port fixture. No
// browser, Core request, provider API or local database is used by this test.
const source = readFileSync(new URL("../local.deepthink-ui/ui/app.js", import.meta.url), "utf8")
  .replace(/^import .*;\r?$/gm, "");
const runId = "a".repeat(64);
const storeShape = "local.harness-chat.conversation-store@1.0";
const inputShape = "local.harness-chat.user-input@1.0";
const continuationShape = "local.deepthink.continuation-input@1.0";
const assistantShape = "local.harness-chat.assistant-output@1.0";

class Element {
  constructor() {
    this.textContent = "";
    this.value = "";
    this.dataset = {};
    this.children = [];
    this.listeners = new Map();
  }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  addEventListener(name, listener) { this.listeners.set(name, listener); }
}

function receipt(count = 1) {
  return {
    version: "deepthink.workflow.v5", run_id: runId, conversation: "e2e",
    status: "paused", approved: false, next_phase: "goal_contract", completed_phases: count,
    usage: { pending_attempts: 0, tokens: { input_tokens: count * 10, output_tokens: count * 5 }, tokens_complete: true, cost_known: false },
  };
}

const emitted = (value) => ({ kind: { kind: "emitted", shape: assistantShape, value } });
const end = { kind: { kind: "completed" } };
function pauseEvents(value = receipt(), flat = false) {
  const values = [
    { kind: "progress", text: JSON.stringify({ version: value.version, run_id: runId, phase: "goal_intake", status: "completed", usage: value.usage }) },
    { kind: "usage", text: JSON.stringify(value.usage) },
    { kind: "progress", text: JSON.stringify(value) },
    { kind: "paused", text: JSON.stringify(value) },
  ];
  return flat ? [...values, { kind: "completed" }] : [...values.map(emitted), end];
}

async function harness(segments) {
  const elements = new Map();
  const element = (selector) => {
    if (!elements.has(selector)) elements.set(selector, new Element());
    return elements.get(selector);
  };
  const calls = [];
  let messages = [];
  let index = 0;
  let cancelled = 0;
  const client = {
    async call(shape, op, args) {
      calls.push({ shape, op, args });
      if (shape === storeShape) {
        assert.equal(op, "load");
        return { conversation: "e2e", messages: structuredClone(messages) };
      }
      assert.ok(shape === inputShape || shape === continuationShape);
      assert.equal(op, "submit");
      const segment = segments[index++];
      assert.ok(segment, "no automatic extra User request is allowed");
      let delivered = false;
      return {
        async pull(max) {
          assert.equal(max, 8);
          assert.equal(delivered, false);
          delivered = true;
          if (segment.persisted) messages = structuredClone(segment.persisted);
          return segment.events;
        },
        async cancel() { cancelled += 1; },
      };
    },
  };
  const context = {
    document: { querySelector: element, createElement: () => new Element() },
    sessionStorage: { getItem: () => null, setItem: () => {} },
    connectPackUi: async () => client,
    formatUsage, parseEventText,
  };
  runInNewContext(source, context);
  await new Promise((resolve) => setImmediate(resolve));
  return { context, elements, calls, cancelled: () => cancelled };
}

test("a paused collected Flow segment is visible, resumable and never a saved answer", async () => {
  for (const flat of [false, true]) {
    const h = await harness([{ events: pauseEvents(receipt(), flat) }]);
    await h.context.submit("課題");
    const articles = h.elements.get("#messages").children;
    assert.equal(articles[1].children[0].textContent, "");
    assert.match(articles[1].children[1].textContent, /一時停止中/);
    assert.match(h.elements.get("#status").textContent, /1工程を保存/);
    assert.equal(h.elements.get("#resume").disabled, false);
    assert.equal(h.calls.filter((call) => call.shape === inputShape).length, 1);
    assert.equal(h.calls.filter((call) => call.shape === continuationShape).length, 0);
    assert.equal(h.cancelled(), 1);
  }
});

test("only an explicit resume submits the original run through continuation-input", async () => {
  const h = await harness([{ events: pauseEvents() }, { events: pauseEvents(receipt(2)) }]);
  await h.context.submit("課題");
  assert.equal(h.calls.filter((call) => call.shape === continuationShape).length, 0);
  await h.elements.get("#resume").listeners.get("click")();
  const continuation = h.calls.filter((call) => call.shape === continuationShape);
  assert.equal(continuation.length, 1);
  assert.equal(continuation[0].args["run-id"], runId);
  assert.equal(continuation[0].args.conversation, "e2e");
  assert.equal(continuation[0].args.answers, null);
  assert.match(h.elements.get("#usage").textContent, /入力 20 \/ 出力 10/);
  assert.equal(h.elements.get("#resume").disabled, false);
});

test("ordinary completed Harness output still requires and confirms the stored answer", async () => {
  const h = await harness([{
    events: [
      emitted({ kind: "delta", text: "391" }), emitted({ kind: "done", text: "391" }),
      emitted({ kind: "progress", text: JSON.stringify({ run_id: runId, status: "completed", approved: true }) }), end,
    ],
    persisted: [{ role: "user", content: "課題" }, { role: "assistant", content: "391" }],
  }]);
  await h.context.submit("課題");
  const assistant = h.elements.get("#messages").children[1];
  assert.equal(assistant.children[0].textContent, "391");
  assert.equal(assistant.children[1].textContent, "✓ 保存済み");
  assert.equal(h.elements.get("#resume").disabled, true);
});

test("mixed answer and pause cannot be accepted as a successful suspended segment", async () => {
  const events = pauseEvents();
  events.splice(0, 0, emitted({ kind: "delta", text: "unapproved candidate" }));
  const h = await harness([{ events }]);
  await h.context.submit("課題");
  assert.match(h.elements.get("#status").textContent, /安全な中断を確認できません/);
  assert.equal(h.elements.get("#resume").disabled, true);
  assert.equal(h.calls.filter((call) => call.shape === continuationShape).length, 0);
});

test("unsettled or mismatched pause receipts never cause automatic continuation", async () => {
  for (const field of ["conversation", "approved", "usage"]) {
    const value = receipt();
    value[field] = field === "conversation" ? "another" : field === "approved" ? true : { pending_attempts: 1 };
    const h = await harness([{ events: pauseEvents(value) }]);
    await h.context.submit("課題");
    assert.match(h.elements.get("#status").textContent, /安全な中断を確認できません/);
    assert.equal(h.elements.get("#resume").disabled, true);
    assert.equal(h.calls.filter((call) => call.shape === continuationShape).length, 0);
  }
});

test("unexpected persisted messages after pause block the UI continuation control", async () => {
  const h = await harness([{
    events: pauseEvents(),
    persisted: [{ role: "user", content: "課題" }, { role: "assistant", content: "unexpected answer" }],
  }]);
  await h.context.submit("課題");
  assert.match(h.elements.get("#status").textContent, /回答として保存されていないことを確認できません/);
  assert.equal(h.elements.get("#resume").disabled, true);
  assert.equal(h.calls.filter((call) => call.shape === continuationShape).length, 0);
});
