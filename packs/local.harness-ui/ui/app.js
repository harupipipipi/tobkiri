import { connectPackUi } from "./ports.js";

const inputShape = "local.harness-chat.user-input@1.0";
const storeShape = "local.harness-chat.conversation-store@1.0";
const status = document.querySelector("#status");
const messages = document.querySelector("#messages");
const form = document.querySelector("#composer");
const text = document.querySelector("#text");
const send = document.querySelector("#send");
const stop = document.querySelector("#stop");
const conversation = "e2e";
let client;
let active;
let submitting = false;
let stopping;

const stoppedStatus = "停止しました。下書きを残しています。";
const stopUnconfirmedStatus = "停止を確認できませんでした。下書きを残しています。";

const message = (role, content, receipt) => {
  const article = document.createElement("article");
  article.dataset.role = role;
  const body = document.createElement("span");
  body.textContent = content;
  const state = document.createElement("small");
  state.textContent = receipt;
  article.append(body, state);
  messages.append(article);
  return { article, body, state };
};

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!client || submitting || !text.value.trim()) return;
  submitting = true;
  const input = text.value;
  const user = message("user", input, "◷ 保存待ち");
  const assistant = message("assistant", "", "生成中");
  send.disabled = true;
  status.textContent = "会話を処理しています";
  let saved = false;
  let failureStatus = "会話を完了できませんでした。下書きを残しています。";
  try {
    const previous = await client.call(storeShape, "load", { conversation });
    const stream = await client.call(inputShape, "submit", { input: { conversation, text: input } });
    active = stream;
    stop.disabled = false;
    let terminal;
    while (!terminal) {
      let events;
      try {
        events = await stream.pull(8);
      } catch (error) {
        if (!stopping) throw error;
        events = [];
      }
      for (const event of events) {
        const frame = event.kind;
        if (["completed", "failed", "cancelled"].includes(frame?.kind)) {
          terminal = frame.kind;
          break;
        }
        if (frame?.kind === "emitted" && frame.shape === "local.harness-chat.assistant-output@1.0") {
          const value = frame.value;
          if (value.kind === "delta") assistant.body.textContent += value.text ?? "";
          if (value.kind === "done") assistant.state.textContent = "保存を確認中";
        }
      }
      if (terminal) break;
      if (stopping) {
        status.textContent = await stopping ? stoppedStatus : stopUnconfirmedStatus;
        return;
      }
      if (!events.length) {
        status.textContent = "会話の完了を確認できませんでした。下書きを残しています。";
        return;
      }
    }
    stop.disabled = true;
    if (terminal !== "completed") {
      status.textContent = terminal === "cancelled" ? stoppedStatus : failureStatus;
      return;
    }
    failureStatus = "会話の保存を確認できませんでした。下書きを残しています。";
    const persisted = await client.call(storeShape, "load", { conversation });
    const tail = persisted.messages.slice(-2);
    if (persisted.messages.length < previous.messages.length + 2 || tail[0]?.role !== "user" || tail[0]?.content !== input || tail[1]?.role !== "assistant" || tail[1]?.content !== assistant.body.textContent) {
      status.textContent = failureStatus;
      return;
    }
    user.state.textContent = "✓ 保存済み";
    assistant.state.textContent = "✓ 保存済み";
    saved = true;
    if (text.value === input) text.value = "";
    status.textContent = "準備できました";
  } catch {
    status.textContent = failureStatus;
  } finally {
    stop.disabled = true;
    if (!saved) {
      user.article.remove();
      assistant.article.remove();
    }
    if (active) {
      try { await (stopping ?? active.cancel()); } catch {}
    }
    active = undefined;
    stopping = undefined;
    submitting = false;
    send.disabled = false;
  }
});
stop.addEventListener("click", () => {
  if (!active || stop.disabled) return;
  stop.disabled = true;
  status.textContent = "停止しています";
  stopping = active.cancel().then(() => true, () => false);
});
connectPackUi().then((connection) => {
  client = connection;
  send.disabled = false;
  status.textContent = "準備できました";
}).catch(() => {
  status.textContent = "ホストに接続できませんでした。画面を再読み込みしてください。";
});
