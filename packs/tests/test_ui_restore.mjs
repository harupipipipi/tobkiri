import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { runInNewContext } from "node:vm";
import { formatUsage, parseEventText } from "../local.deepthink-ui/ui/accounting.mjs";
import * as restore from "../local.deepthink-ui/ui/restore.mjs";

// The actual app, a memory DOM and fake typed ports only. No browser/host/model.
const source = readFileSync(new URL("../local.deepthink-ui/ui/app.js", import.meta.url), "utf8")
  .replace(/^import .*;\r?$/gm, "");
const html = readFileSync(new URL("../local.deepthink-ui/ui/index.html", import.meta.url), "utf8");
const buttonTypes = new Map([...html.matchAll(/<button\b[^>]*\bid="([^"]+)"[^>]*\btype="([^"]+)"/g)]
  .map((match) => [`#${match[1]}`, match[2]]));
const RUN = "a".repeat(64);
const OTHER = "b".repeat(64);
const SHAPE = "local.deepthink.continuation-input@1.0";
const STORE = "local.harness-chat.conversation-store@1.0";
const tick = () => new Promise((resolve) => setImmediate(resolve));

class Element {
  textContent = "";
  value = "";
  dataset = {};
  children = [];
  listeners = new Map();
  formSubmissionAttempts = 0;
  constructor(selector) { this.type = buttonTypes.get(selector); }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.children = items; }
  addEventListener(name, listener) { this.listeners.set(name, listener); }
  async click() {
    if (this.disabled) return;
    const event = { defaultPrevented: false, preventDefault() { this.defaultPrevented = true; } };
    await this.listeners.get("click")?.(event);
    // An allow-scripts-only frame blocks this before any form submit event.
    if (this.type === "submit" && !event.defaultPrevented) {
      this.formSubmissionAttempts += 1;
      throw new Error("sandbox blocks the form submission algorithm");
    }
  }
}

function usage() {
  return { attempts: 1, pending_attempts: 0, tokens_unconfirmed_attempts: 0,
    content_incomplete_attempts: 0, invalid_resolution_attempts: 0,
    tokens_complete: true, tokens_overflow_fields: [], cost_known: true,
    cost_usd: "0.00100000000000000001", cost_receipts_usd: ["0.00100000000000000001"],
    tokens: { input_tokens: 10, output_tokens: 5, total_tokens: 15,
      cached_input_tokens: 0, reasoning_tokens: 2 } };
}

function snapshot(state = "paused") {
  return { version: "deepthink.workflow.v5", run_id: RUN, status: state,
    repository: { format: "deepthink.run-ledger.v1", run_id: RUN, run_request_digest: RUN,
      revision: "5", snapshot_digest: "c".repeat(64) },
    phases: [{ phase: "goal_intake", status: "completed", result: { summary: "保存済み" } }],
    goal: { clarifications: [] }, usage: usage(), error: null, reconciliation: null };
}

function retrySnapshot() {
  const value = snapshot("awaiting_retry");
  value.phases = [];
  value.retry_boundary = { version: "deepthink.workflow.v5", retry_boundary_version: "deepthink.contract-retry-boundary.v1",
    run_id: RUN, conversation: "restored", status: "awaiting_retry", approved: false,
    phase: "goal_intake", next_phase: "goal_intake", completed_phases: 0,
    completed_phases_this_segment: 0, reason: "invalid_phase_contract",
    rejected_attempt: "goal_intake@0:0#0:0", rejected_generation: "goal_intake@0:0#0",
    prepared_generation: "goal_intake@0:0#1", retry: 1, maximum_retries: 2, usage: usage() };
  return value;
}

async function harness({ reply = snapshot(), cached = null, storageThrows = false } = {}) {
  const elements = new Map();
  const element = (id) => {
    if (!elements.has(id)) elements.set(id, new Element(id));
    return elements.get(id);
  };
  const calls = [];
  const writes = [];
  const client = {
    async call(shape, op, args, options) {
      assert.equal(options, restore.REQUEST_OPTIONS, "every port wait has the existing explicit bound");
      calls.push({ shape, op, args: structuredClone(args), options });
      if (shape === SHAPE && op === "status") {
        if (reply instanceof Error) throw reply;
        return typeof reply === "function" ? await reply() : structuredClone(reply);
      }
      if (shape === STORE && op === "load") return { messages: [] };
      assert.equal(shape, SHAPE, "restoration must never dispatch a fresh user-input/model");
      assert.equal(op, "submit");
      const receipt = { version: "deepthink.workflow.v5", run_id: args["run-id"],
        conversation: args.conversation, status: "paused", approved: false,
        completed_phases: 2, next_phase: "goal_contract", usage: usage() };
      let delivered = false;
      return {
        async pull(max, options) {
          assert.equal(max, 8);
          assert.equal(options, restore.REQUEST_OPTIONS);
          assert.equal(delivered, false);
          delivered = true;
          const emitted = (value) => ({ kind: { kind: "emitted", shape: "local.harness-chat.assistant-output@1.0", value } });
          return [emitted({ kind: "progress", text: JSON.stringify(receipt) }),
            emitted({ kind: "paused", text: JSON.stringify(receipt) }), { kind: { kind: "completed" } }];
        },
        async cancel() {},
      };
    },
  };
  const context = { document: { querySelector: element, createElement: () => new Element() },
    sessionStorage: {
      getItem() { if (storageThrows) throw new Error("opaque frame"); return cached === null ? null : JSON.stringify(cached); },
      setItem(key, value) { if (storageThrows) throw new Error("opaque frame"); writes.push({ key, value: JSON.parse(value) }); },
    }, connectPackUi: async () => client, formatUsage, parseEventText, ...restore };
  runInNewContext(source, context);
  await tick();
  return { element, calls, writes, context,
    async read(id = RUN, conversation = "restored") {
      element("#restore-run").value = id;
      element("#restore-conversation").value = conversation;
      await element("#restore").click();
    },
    async resume() { await element("#resume").listeners.get("click")(); },
    async compose(text = "answer") {
      element("#text").value = text;
      await element("#send").click();
    },
    async key(selector, options = {}) {
      const event = { key: "Enter", target: element(selector), defaultPrevented: false,
        preventDefault() { this.defaultPrevented = true; }, ...options };
      const form = selector.startsWith("#restore") ? "#restore-form" : "#composer";
      await element(form).listeners.get("keydown")(event);
      return event;
    },
    async submitForm(selector) {
      const event = { defaultPrevented: false, preventDefault() { this.defaultPrevented = true; } };
      await element(selector).listeners.get("submit")(event);
      return event;
    },
  };
}

test("sandboxed restoration uses a button click without any form submission", async () => {
  const h = await harness();
  assert.equal(h.element("#restore").type, "button");
  assert.equal(h.element("#send").type, "button");
  await h.read();
  assert.equal(h.calls.length, 1);
  assert.equal(h.calls[0].op, "status");
  assert.equal(h.element("#restore").formSubmissionAttempts, 0);
  assert.equal((await h.submitForm("#restore-form")).defaultPrevented, true);
  assert.equal(h.calls.length, 1, "a synthetic form event is not a second effect entry");
});

test("restore input Enter uses the same action; IME and repeated keys do not dispatch", async () => {
  for (const selector of ["#restore-run", "#restore-conversation", "#restore"]) {
    const h = await harness();
    h.element("#restore-run").value = RUN;
    h.element("#restore-conversation").value = "restored";
    for (const options of [{ isComposing: true }, { keyCode: 229 }, { repeat: true }]) {
      await h.key(selector, options);
      assert.equal(h.calls.length, 0);
    }
    if (selector === "#restore") {
      for (const modifier of [{ altKey: true }, { shiftKey: true }, { altKey: true, shiftKey: true }]) {
        for (const options of [{ isComposing: true }, { keyCode: 229 }, { repeat: true }]) {
          assert.equal((await h.key(selector, { ...modifier, ...options })).defaultPrevented, true);
          assert.equal(h.calls.length, 0);
        }
      }
    }
    assert.equal((await h.key(selector)).defaultPrevented, true);
    assert.equal(h.calls.length, 1);
    assert.equal(h.element("#resume").disabled, false);
  }
});

test("click and Enter cannot duplicate a busy STATUS; invalid click still revokes resume", async () => {
  let release;
  const h = await harness({ reply: () => new Promise((resolve) => { release = resolve; }) });
  const pending = h.read();
  await h.key("#restore-conversation");
  await h.element("#restore").click();
  await h.submitForm("#restore-form");
  assert.equal(h.calls.length, 1);
  release(snapshot());
  await pending;
  const saved = structuredClone(h.writes);
  assert.equal(h.element("#resume").disabled, false);
  await h.read("invalid", "restored");
  assert.equal(h.calls.length, 1);
  assert.equal(h.element("#resume").disabled, true);
  assert.deepEqual(h.writes, saved);
});

test("a cache is a visible unverified reference, even with forged status/authority flags", async () => {
  const h = await harness({ cached: { runId: RUN, conversation: "cached", runStatus: "paused", verified: true, canResume: true } });
  assert.equal(h.calls.length, 0);
  assert.equal(h.element("#restore-run").value, RUN);
  assert.equal(h.element("#restore-conversation").value, "cached");
  assert.equal(runInNewContext("conversation", h.context), "e2e", "cache only prefills, never adopts conversation");
  assert.match(h.element("#status").textContent, /未検証/);
  assert.equal(h.element("#resume").disabled, true);
  assert.equal(h.element("#send").disabled, true);
  await h.resume(); await h.compose();
  assert.equal(h.calls.length, 0);
  assert.equal(h.writes.length, 0);
});

test("manual restoration works with blocked storage, one STATUS and no history/submit", async () => {
  const h = await harness({ storageThrows: true });
  await h.read();
  assert.equal(h.calls.length, 1);
  assert.deepEqual(h.calls[0].args, { "run-id": RUN });
  assert.equal(h.calls[0].shape, SHAPE); assert.equal(h.calls[0].op, "status");
  assert.equal(h.element("#resume").disabled, false);
  assert.equal(h.element("#send").disabled, true);
  assert.equal(h.element("#messages").children.length, 0);
  assert.match(h.element("#run-reference").textContent, /入力値・再開時に照合/);
  assert.match(h.element("#usage").textContent, /0\.00100000000000000001/);
});

test("only a separate manual resume sends same Run/conversation, never a fresh submit", async () => {
  const h = await harness();
  await h.read();
  assert.equal(h.calls.length, 1);
  await h.resume();
  const submits = h.calls.filter((call) => call.op === "submit");
  assert.equal(submits.length, 1);
  assert.equal(submits[0].shape, SHAPE);
  assert.deepEqual(submits[0].args, { "run-id": RUN, conversation: "restored", answers: null });
  assert.match(h.element("#status").textContent, /1工程を保存/);
});

test("successful STATUS adopts the entered conversation exactly, without trimming its identifier", async () => {
  const h = await harness();
  const entered = "  original-conversation  ";
  await h.read(RUN, entered);
  assert.equal(h.writes.at(-1).value.conversation, entered);
  await h.resume();
  assert.equal(h.calls.find((call) => call.op === "submit").args.conversation, entered);
});

test("bad/foreign replies and transport failure preserve old cache/ref and disable all resume paths", async () => {
  for (const reply of [new Error("timed out"), { ...snapshot(), run_id: OTHER }, [], null, "bad"]) {
    const h = await harness({ reply, cached: { runId: OTHER, conversation: "old", runStatus: "paused" } });
    await h.read();
    assert.equal(h.writes.length, 0);
    assert.match(h.element("#run-reference").textContent, new RegExp(OTHER));
    assert.equal(h.element("#resume").disabled, true);
    assert.equal(h.element("#send").disabled, true);
    await h.resume(); await h.compose();
    assert.equal(h.calls.length, 1);
    assert.match(h.element("#status").textContent, /以前の参照は保持/);
  }
});

test("restore displays only a fixed closed error classification without private detail", async () => {
  for (const code of [
    "approval-required", "blocked", "cancelled", "closed", "conflict",
    "consumed", "deadline", "denied", "duplicate", "internal", "invalid",
    "limit", "limit-exceeded", "not-found", "persistence", "registration-full",
    "stale-epoch", "trap", "unavailable",
  ]) {
    const reply = Object.assign(new Error("private-body"), { code, payload: "private-payload" });
    const h = await harness({ reply, cached: { runId: OTHER, conversation: "old" } });
    await h.read();
    assert.equal(h.element("#status").textContent,
      `照会を完了できませんでした。以前の参照は保持しています。再開せず確認してください。 エラー分類: ${code}。 `);
    assert.doesNotMatch(h.element("#status").textContent, /private-/);
    assert.equal(h.element("#resume").disabled, true);
    assert.equal(h.element("#send").disabled, true);
    assert.equal(h.writes.length, 0);
    assert.equal(h.calls.length, 1);
  }
});

test("unknown or malformed error classification never substitutes for saved status", async () => {
  for (const code of [undefined, null, 0, {}, "NOT_FOUND", "not_found", "provider-private", "not-found\n", "x".repeat(65)]) {
    const reply = Object.assign(new Error("private-body"), { code });
    const h = await harness({ reply, cached: { runId: OTHER, conversation: "old" } });
    await h.read(); await h.resume(); await h.compose();
    assert.equal(h.element("#status").textContent,
      "照会を完了できませんでした。以前の参照は保持しています。再開せず確認してください。 エラー分類: unknown。 ");
    assert.equal(h.calls.length, 1);
    assert.equal(h.writes.length, 0);
    assert.equal(h.element("#resume").disabled, true);
    assert.equal(h.element("#send").disabled, true);
  }
  const value = snapshot("unknown");
  value.code = "not-found";
  const h = await harness({ reply: value }); await h.read();
  assert.match(h.element("#status").textContent, /エラー分類: unknown/);
  assert.equal(h.element("#resume").disabled, true);
  assert.equal(h.writes.length, 0);
});

test("a later typed refusal revokes verified Resume without replacing the old reference", async () => {
  let failed = false;
  const h = await harness({ reply: () => {
    if (failed) throw Object.assign(new Error("Core refused the UI request"), { code: "not-found" });
    return snapshot();
  } });
  await h.read();
  assert.equal(h.element("#resume").disabled, false);
  const saved = structuredClone(h.writes);
  failed = true;
  await h.read(OTHER, "another"); await h.resume(); await h.compose();
  assert.match(h.element("#status").textContent, /エラー分類: not-found。 Core refused the UI request/);
  assert.equal(h.calls.length, 2);
  assert.deepEqual(h.writes, saved);
  assert.match(h.element("#run-reference").textContent, new RegExp(RUN));
  assert.equal(h.element("#resume").disabled, true);
  assert.equal(h.element("#send").disabled, true);
});

test("repository/version/types/counters/decimal corruption cannot enable Resume", async () => {
  for (const mutate of [
    (v) => { v.version = "future"; }, (v) => { v.repository = null; },
    (v) => { v.repository.run_id = OTHER; }, (v) => { v.repository.run_request_digest = OTHER; },
    (v) => { v.repository.revision = 5; }, (v) => { v.repository.revision = "05"; },
    (v) => { v.repository.snapshot_digest = "bad"; }, (v) => { v.status = "future"; },
    (v) => { v.usage.pending_attempts = "0"; }, (v) => { v.usage.tokens.output_tokens = 1.5; },
    (v) => { v.usage.tokens.input_tokens = -1; }, (v) => { v.usage.tokens_complete = "true"; },
    (v) => { v.usage.cost_usd = "NaN"; }, (v) => { v.usage.cost_known = false; },
    (v) => { v.phases = [{ phase: "x", status: "running" }]; },
  ]) {
    const value = snapshot(); mutate(value);
    const h = await harness({ reply: value }); await h.read();
    assert.equal(h.element("#resume").disabled, true);
    assert.equal(h.writes.length, 0);
    assert.equal(h.calls.length, 1);
  }
});

test("running/completed/quarantined/reconciliation diagnostics never offer ordinary resume or answer", async () => {
  for (const state of ["running", "completed", "quarantined", "needs_reconciliation"]) {
    const h = await harness({ reply: snapshot(state) }); await h.read();
    assert.equal(h.element("#resume").disabled, true);
    assert.equal(h.element("#send").disabled, true);
    assert.equal(h.element("#messages").children.length, 0);
    assert.match(h.element("#phase").textContent, new RegExp(state));
  }
});

test("Core pending/reconciliation/error state never authorizes continuation", async () => {
  for (const mutate of [
    (v) => { v.usage.pending_attempts = 1; },
    (v) => { v.error = { code: "checkpoint-unconfirmed", message: "not confirmed" }; },
    (v) => { v.reconciliation = { code: "needs-reconciliation", message: "pending" }; },
  ]) {
    const value = snapshot(); mutate(value);
    const h = await harness({ reply: value }); await h.read(); await h.resume();
    assert.equal(h.calls.length, 1);
    assert.equal(h.element("#resume").disabled, true);
    assert.match(h.element("#next-action").textContent, /未確定/);
  }
});

test("unknown accounting remains unknown without inventing a reconciliation requirement", async () => {
  for (const mutate of [
    (v) => { v.usage.cost_known = false; v.usage.cost_usd = null; v.usage.cost_receipts_usd = []; },
    (v) => { v.usage.tokens_complete = false; v.usage.tokens_unconfirmed_attempts = 1; v.usage.tokens = {}; },
    (v) => { v.usage.tokens_complete = false; v.usage.tokens_overflow_fields = ["input_tokens"]; delete v.usage.tokens.input_tokens; },
  ]) {
    const value = snapshot(); mutate(value);
    const h = await harness({ reply: value }); await h.read();
    assert.equal(h.element("#resume").disabled, false);
    assert.match(h.element("#usage").textContent, /不明/);
    assert.doesNotMatch(h.element("#next-action").textContent, /再開せず/);
    assert.equal(h.calls.length, 1);
  }
});

test("paused needs at least one completed phase; a valid zero-phase retry is distinct", async () => {
  const empty = snapshot(); empty.phases = [];
  const bad = await harness({ reply: empty }); await bad.read();
  assert.equal(bad.element("#resume").disabled, true); assert.equal(bad.writes.length, 0);
  const good = await harness({ reply: retrySnapshot() }); await good.read();
  assert.equal(good.element("#resume").disabled, false);
  assert.match(good.element("#phase").textContent, /awaiting_retry · 保存済み 0工程/);
});

test("retry must bind the entered conversation, same Run, exact next epoch and matching usage", async () => {
  for (const mutate of [
    (v) => { v.retry_boundary.conversation = "foreign"; },
    (v) => { v.retry_boundary.run_id = OTHER; },
    (v) => { v.retry_boundary.completed_phases = 1; },
    (v) => { v.retry_boundary.prepared_generation = v.retry_boundary.rejected_generation; },
    (v) => { v.retry_boundary.usage.tokens.input_tokens = 11; },
    (v) => { v.retry_boundary.usage.pending_attempts = 1; },
  ]) {
    const value = retrySnapshot(); mutate(value);
    const h = await harness({ reply: value }); await h.read();
    assert.equal(h.element("#resume").disabled, true); assert.equal(h.writes.length, 0);
  }
});

test("validated needs_input offers only a separate answer on the same Run", async () => {
  const value = snapshot("needs_input"); value.goal.clarifications = ["対象を確認してください"];
  const h = await harness({ reply: value }); await h.read();
  assert.equal(h.element("#resume").disabled, true);
  assert.equal(h.element("#send").disabled, false);
  assert.equal(h.calls.length, 1);
  await h.compose("実際の回答");
  const submitted = h.calls.filter((call) => call.op === "submit");
  assert.equal(submitted.length, 1);
  assert.deepEqual(submitted[0].args, { "run-id": RUN, conversation: "restored", answers: "実際の回答" });
  const noQuestions = await harness({ reply: snapshot("needs_input") }); await noQuestions.read();
  assert.equal(noQuestions.element("#send").disabled, true);
});

test("double clicks are serialized; changed inputs invalidate a late reply without cache overwrite", async () => {
  let finish;
  const pending = new Promise((resolve) => { finish = resolve; });
  const h = await harness({ reply: () => pending, cached: { runId: OTHER, conversation: "old" } });
  const first = h.read(); await tick();
  assert.equal(h.element("#restore").disabled, true);
  await h.read(); await h.resume(); await h.compose();
  assert.equal(h.calls.length, 1);
  h.element("#restore-run").value = OTHER;
  finish(snapshot()); await first;
  assert.equal(h.writes.length, 0);
  assert.equal(h.element("#resume").disabled, true);
  assert.match(h.element("#status").textContent, /入力が変わりました/);
});

test("invalid/bounded selection makes no port call; new request requires an explicit reset", async () => {
  for (const [run, conversation] of [["bad", "restored"], [RUN, ""], [RUN, "x".repeat(257)], [RUN, "あ".repeat(86)]]) {
    const h = await harness(); await h.read(run, conversation); await h.compose();
    assert.equal(h.calls.length, 0); assert.equal(h.element("#send").disabled, true);
    await h.element("#new-request").listeners.get("click")();
    assert.equal(h.element("#send").disabled, false);
  }
});

test("lossless public integer and decimal strings stay exact rather than fake zero or float", async () => {
  const value = snapshot();
  Object.assign(value.usage.tokens, { input_tokens: "9007199254740992", output_tokens: "1", total_tokens: "9007199254740993", reasoning_tokens: "0" });
  const h = await harness({ reply: value }); await h.read();
  assert.equal(h.element("#resume").disabled, false);
  assert.match(h.element("#usage").textContent, /9,007,199,254,740,992/);
  assert.match(h.element("#usage").textContent, /\$0\.00100000000000000001/);
});
