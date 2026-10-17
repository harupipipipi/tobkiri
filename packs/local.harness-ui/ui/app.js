import { connectPackUi } from "./ports.js";
import { formatUsage, parseEventText } from "./accounting.mjs";

const inputShape = "local.harness-chat.user-input@1.0";
const storeShape = "local.harness-chat.conversation-store@1.0";
const status = document.querySelector("#status");
const messages = document.querySelector("#messages");
const form = document.querySelector("#composer");
const text = document.querySelector("#text");
const send = document.querySelector("#send");
const stop = document.querySelector("#stop");
const resume = document.querySelector("#resume");
const workflow = document.querySelector("#workflow");
const phase = document.querySelector("#phase");
const usage = document.querySelector("#usage");
const runReference = document.querySelector("#run-reference");
const nextAction = document.querySelector("#next-action");
const phaseLog = document.querySelector("#phase-log");
const continuationShape = "local.deepthink.continuation-input@1.0";
const conversation = "e2e";
let client;
let active;
let cancelled = false;
let busy = false;
let runId;
let runStatus;
// This is only a resume reference. The authoritative state remains in Core.
// Sandboxed hosts may disallow browser storage; live continuation still works.
try {
  const saved = JSON.parse(sessionStorage.getItem("tobkiri.harness.resume") ?? "null");
  if (/^[a-f0-9]{64}$/.test(saved?.runId ?? "")) {
    runId = saved.runId;
    runStatus = saved.runStatus;
    workflow.hidden = false;
    runReference.textContent = `再開参照: ${runId}`;
  }
} catch {}

function rememberRun() {
  try { sessionStorage.setItem("tobkiri.harness.resume", JSON.stringify({ runId, runStatus })); } catch {}
}

function progress(value) {
  workflow.hidden = false;
  if (/^[a-f0-9]{64}$/.test(value.run_id ?? "")) runId = value.run_id;
  if (typeof value.status === "string") {
    runStatus = value.phase && value.status === "completed" ? "running" : value.status;
  }
  phase.textContent = `${value.phase ?? "DeepThink"} · ${value.status ?? "実行中"}`;
  nextAction.textContent = value.summary?.next_action ?? "";
  if (value.summary && value.phase) {
    const item = document.createElement("li");
    const summary = value.summary;
    const assumptions = Array.isArray(summary.assumptions) ? summary.assumptions.join(" / ") : "";
    item.textContent = `${value.phase}: ${summary.objective ?? ""}\n判断: ${summary.decision ?? ""}\n次の作業: ${summary.next_action ?? ""}${assumptions ? `\n仮定: ${assumptions}` : ""}`;
    phaseLog.append(item);
  }
  if (runId) runReference.textContent = `再開参照: ${runId}`;
  if (value.usage) usage.textContent = formatUsage(value.usage);
  if (value.error?.message) nextAction.textContent = value.error.message;
  rememberRun();
}

const message = (role, content, receipt) => {
  const article = document.createElement("article");
  article.dataset.role = role;
  const body = document.createElement("span");
  body.textContent = content;
  const state = document.createElement("small");
  state.textContent = receipt;
  article.append(body, state);
  messages.append(article);
  return { body, state };
};

async function submit(input, continuation = false) {
  if (!client || busy) return;
  if (continuation && !runId) return;
  busy = true;
  if (!continuation) {
    runId = undefined;
    runStatus = undefined;
    rememberRun();
    workflow.hidden = true;
    phaseLog.replaceChildren();
  }
  const originalInput = input;
  input = input ?? "チェックポイントから再開";
  const user = message("user", input, "◷ 保存待ち");
  const assistant = message("assistant", "", "生成中");
  send.disabled = true;
  resume.disabled = true;
  cancelled = false;
  try {
    const previous = await client.call(storeShape, "load", { conversation });
    const stream = continuation
      ? await client.call(continuationShape, "submit", { "run-id": runId, conversation, answers: originalInput })
      : await client.call(inputShape, "submit", { input: { conversation, text: input } });
    active = stream;
    if (cancelled) throw new Error("停止しました。下書きを残しています。");
    stop.disabled = false;
    let completed = false;
    while (active && !completed) {
      const events = await stream.pull(8);
      if (!events.length) throw new Error("会話の完了を確認できませんでした。下書きを残しています。");
      for (const event of events) {
        const frame = event.kind;
        if (frame?.kind === "failed") throw new Error("会話を完了できませんでした。下書きを残しています。");
        if (frame?.kind === "cancelled") throw new Error("停止しました。下書きを残しています。");
        if (frame?.kind === "completed") completed = true;
        if (frame?.kind === "emitted" && frame.shape === "local.harness-chat.assistant-output@1.0") {
          const value = frame.value;
          if (value.kind === "delta") assistant.body.textContent += value.text ?? "";
          if (value.kind === "done") assistant.state.textContent = "保存を確認中";
          if (value.kind === "progress") progress(parseEventText(value.text));
          if (value.kind === "usage") {
            workflow.hidden = false;
            usage.textContent = formatUsage(parseEventText(value.text), value["prompt-tokens"], value["completion-tokens"]);
          }
        }
      }
    }
    if (cancelled) throw new Error("停止しました。下書きを残しています。");
    if (!completed) throw new Error("会話の完了を確認できませんでした。下書きを残しています。");
    const persisted = await client.call(storeShape, "load", { conversation });
    const tail = persisted.messages.slice(-2);
    if (persisted.messages.length < previous.messages.length + 2 || tail[0]?.role !== "user" || (!continuation && tail[0]?.content !== input) || tail[1]?.role !== "assistant" || tail[1]?.content !== assistant.body.textContent) {
      throw new Error("会話の保存を確認できませんでした。下書きを残しています。");
    }
    user.state.textContent = "✓ 保存済み";
    assistant.state.textContent = "✓ 保存済み";
    text.value = "";
    status.textContent = runStatus === "needs_input" ? "確認事項への回答を入力してください。途中の成果は保存済みです。" : "準備できました";
  } catch (error) {
    assistant.state.textContent = "送信を完了できませんでした";
    status.textContent = error.message;
    // Draft remains in the composer when the Flow fails or is cancelled.
  } finally {
    if (active) {
      try { await active.cancel(); } catch {}
    }
    active = undefined;
    busy = false;
    stop.disabled = true;
    send.disabled = false;
    resume.disabled = !runId || runStatus === "completed" || runStatus === "needs_input";
  }
}
form.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!text.value.trim()) return;
  await submit(text.value, runStatus === "needs_input" && !!runId);
});
resume.addEventListener("click", () => submit(null, true));
document.querySelector("#new-request").addEventListener("click", () => {
  if (busy) return;
  runId = undefined;
  runStatus = undefined;
  rememberRun();
  resume.disabled = true;
  workflow.hidden = true;
  status.textContent = "新しい依頼を入力できます。以前の成果はCoreに保存されています。";
});
stop.addEventListener("click", async () => {
  cancelled = true;
  if (active) await active.cancel();
  active = undefined;
  status.textContent = "停止しました";
});
connectPackUi().then((connection) => {
  client = connection;
  send.disabled = false;
  resume.disabled = !runId || runStatus === "completed" || runStatus === "needs_input";
  status.textContent = "準備できました";
});
