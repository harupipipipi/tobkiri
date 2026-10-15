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
let cancelled = false;

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

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!client || active || !text.value.trim()) return;
  const input = text.value;
  const user = message("user", input, "◷ 保存待ち");
  const assistant = message("assistant", "", "生成中");
  send.disabled = true;
  cancelled = false;
  try {
    const previous = await client.call(storeShape, "load", { conversation });
    const stream = await client.call(inputShape, "submit", { input: { conversation, text: input } });
    active = stream;
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
        }
      }
    }
    if (cancelled) throw new Error("停止しました。下書きを残しています。");
    if (!completed) throw new Error("会話の完了を確認できませんでした。下書きを残しています。");
    const persisted = await client.call(storeShape, "load", { conversation });
    const tail = persisted.messages.slice(-2);
    if (persisted.messages.length < previous.messages.length + 2 || tail[0]?.role !== "user" || tail[0]?.content !== input || tail[1]?.role !== "assistant" || tail[1]?.content !== assistant.body.textContent) {
      throw new Error("会話の保存を確認できませんでした。下書きを残しています。");
    }
    user.state.textContent = "✓ 保存済み";
    assistant.state.textContent = "✓ 保存済み";
    text.value = "";
    status.textContent = "準備できました";
  } catch (error) {
    assistant.state.textContent = "送信を完了できませんでした";
    status.textContent = error.message;
    // Draft remains in the composer when the Flow fails or is cancelled.
  } finally {
    if (active) {
      try { await active.cancel(); } catch {}
    }
    active = undefined;
    stop.disabled = true;
    send.disabled = false;
  }
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
  status.textContent = "準備できました";
});
