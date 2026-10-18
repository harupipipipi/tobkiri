import { connectPackUi } from "./ports.js";
import { formatUsage, parseEventText } from "./accounting.mjs";
import { REQUEST_OPTIONS, inspectStatus, statusUsage, validConversation, validRetryBoundary, validRunId } from "./restore.mjs";

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
const restoreForm = document.querySelector("#restore-form");
const restoreRun = document.querySelector("#restore-run");
const restoreConversation = document.querySelector("#restore-conversation");
const restoreButton = document.querySelector("#restore");
const continuationShape = "local.deepthink.continuation-input@1.0";
const PORT_ERROR_CODES = new Set([
  "approval-required", "blocked", "cancelled", "closed", "conflict",
  "consumed", "deadline", "denied", "duplicate", "internal", "invalid",
  "limit", "limit-exceeded", "not-found", "persistence", "registration-full",
  "stale-epoch", "trap", "unavailable",
]);
const RESTORE_ERROR_MESSAGES = new Set([
  "Core refused the UI request", "Invalid UI request", "UI request refused",
  "Core response exceeded the UI limit", "Port call failed",
  "保存状態の形式または参照が一致しません。再送せず参照を保持してください。",
  "照会中に参照入力が変わりました。以前の参照を保持し、再開は無効のままです。",
]);

function restoreErrorLabel(error) {
  const code = error && typeof error === "object" && Object.getOwnPropertyDescriptor(error, "code")?.value;
  return PORT_ERROR_CODES.has(code) ? code : "unknown";
}

let conversation = "e2e";
let client;
let active;
let cancelled = false;
let busy = false;
let runId;
let runStatus;
let contextVerified = false;
let canResume = false;
let canAnswer = false;
let referenceBlocked = false;
let restoreGeneration = 0;
restoreConversation.value = conversation;
// This is only a resume reference. The authoritative state remains in Core.
// Sandboxed hosts may disallow browser storage; live continuation still works.
try {
  const saved = JSON.parse(sessionStorage.getItem("tobkiri.harness.resume") ?? "null");
  if (validRunId(saved?.runId)) {
    runId = saved.runId;
    restoreRun.value = runId;
    restoreConversation.value = validConversation(saved.conversation) ? saved.conversation : conversation;
    referenceBlocked = true;
    workflow.hidden = false;
    runReference.textContent = `未検証の保存参照: ${runId}`;
  }
} catch {}

function rememberRun() {
  try { sessionStorage.setItem("tobkiri.harness.resume", JSON.stringify({ runId, runStatus, conversation })); } catch {}
}

function controls() {
  resume.disabled = !client || busy || !contextVerified || !canResume;
  send.disabled = !client || busy || referenceBlocked || (!!runId && !canAnswer);
  restoreButton.disabled = !client || busy;
  restoreRun.disabled = busy;
  restoreConversation.disabled = busy;
  document.querySelector("#new-request").disabled = busy;
}

async function restoreReference() {
  if (!client || busy) return;
  // Keep the old reference/cache until a matching authoritative reply succeeds.
  const selectedRun = restoreRun.value.trim();
  const selectedConversation = restoreConversation.value;
  const generation = ++restoreGeneration;
  contextVerified = canResume = canAnswer = false;
  referenceBlocked = true;
  workflow.hidden = false;
  if (!validRunId(selectedRun) || !validConversation(selectedConversation)) {
    status.textContent = "Run IDは小文字の16進数64桁、会話IDは空欄にせず256バイト以内で入力してください。以前の参照は保持しています。";
    controls();
    return;
  }
  busy = true;
  controls();
  status.textContent = "保存状態を読み取り中です。再開・モデル送信はしていません。";
  try {
    const value = await client.call(continuationShape, "status", { "run-id": selectedRun }, REQUEST_OPTIONS);
    const inspected = inspectStatus(value, selectedRun, selectedConversation);
    if (generation !== restoreGeneration || restoreRun.value.trim() !== selectedRun || restoreConversation.value !== selectedConversation) {
      throw new Error("照会中に参照入力が変わりました。以前の参照を保持し、再開は無効のままです。");
    }
    runId = selectedRun;
    conversation = selectedConversation;
    runStatus = inspected.status;
    contextVerified = true;
    referenceBlocked = false;
    canResume = inspected.resumable;
    canAnswer = inspected.canAnswer;
    runReference.textContent = `再開参照: ${runId}\n会話ID（入力値・再開時に照合）: ${conversation}`;
    phase.textContent = `${runStatus} · 保存済み ${inspected.phaseCount}工程 · revision ${inspected.revision}`;
    usage.textContent = statusUsage(inspected.usage);
    nextAction.textContent = inspected.explanation;
    status.textContent = "保存状態を読み取りました。会話IDは再開時に照合します。";
    rememberRun();
  } catch (error) {
    // No submit/history load/write or cache replacement follows a failed read.
    // A closed transport classification is diagnostic, not a saved Run result.
    const detail = RESTORE_ERROR_MESSAGES.has(error?.message) ? error.message : "";
    status.textContent = `照会を完了できませんでした。以前の参照は保持しています。再開せず確認してください。 エラー分類: ${restoreErrorLabel(error)}。 ${detail}`;
    nextAction.textContent = "再送・自動修正はしていません。明示的に状態を読み直すか「新しい依頼」を選んでください。";
  } finally {
    busy = false;
    controls();
  }
}

function progress(value) {
  workflow.hidden = false;
  if (/^[a-f0-9]{64}$/.test(value.run_id ?? "")) runId = value.run_id;
  if (typeof value.status === "string") {
    runStatus = value.phase && value.status === "completed" ? "running" : value.status;
  }
  if (["needs-reconciliation", "checkpoint-unconfirmed", "core-internal"].includes(value.error?.code)) {
    runStatus = "needs_reconciliation";
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
  if (continuation && (!runId || !contextVerified || (input === null ? !canResume : !canAnswer))) return;
  if (!continuation && (runId || referenceBlocked)) return;
  busy = true;
  contextVerified = canResume = canAnswer = false;
  controls();
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
    const previous = await client.call(storeShape, "load", { conversation }, REQUEST_OPTIONS);
    const stream = continuation
      ? await client.call(continuationShape, "submit", { "run-id": runId, conversation, answers: originalInput }, REQUEST_OPTIONS)
      : await client.call(inputShape, "submit", { input: { conversation, text: input } }, REQUEST_OPTIONS);
    active = stream;
    if (cancelled) throw new Error("停止しました。下書きを残しています。");
    stop.disabled = false;
    let completed = false;
    let pausedSegment = false;
    let retrySegment = false;
    let sawAnswer = false;
    while (active && !completed) {
      const events = await stream.pull(8, REQUEST_OPTIONS);
      if (!events.length) throw new Error("会話の完了を確認できませんでした。下書きを残しています。");
      for (const event of events) {
        const frame = typeof event.kind === "object" ? event.kind : event;
        if (frame?.kind === "failed") throw new Error("会話を完了できませんでした。下書きを残しています。");
        if (frame?.kind === "cancelled") throw new Error("停止しました。下書きを残しています。");
        if (frame?.kind === "completed") completed = true;
        const value = frame?.kind === "emitted" && frame.shape === "local.harness-chat.assistant-output@1.0"
          ? frame.value
          : typeof event.kind === "string" ? event : undefined;
        if (value) {
          if (value.kind === "delta") {
            sawAnswer = true;
            assistant.body.textContent += value.text ?? "";
          }
          if (value.kind === "done") {
            sawAnswer = true;
            assistant.state.textContent = "保存を確認中";
          }
          if (value.kind === "progress") progress(parseEventText(value.text));
          if (value.kind === "paused") {
            const receipt = parseEventText(value.text);
            if (pausedSegment || retrySegment || sawAnswer || receipt.version !== "deepthink.workflow.v5" || receipt.status !== "paused" || receipt.approved !== false || receipt.conversation !== conversation || receipt.run_id !== runId || !Number.isSafeInteger(receipt.completed_phases) || receipt.completed_phases < 1 || typeof receipt.next_phase !== "string" || !receipt.next_phase.trim() || receipt.usage?.pending_attempts !== 0) {
              runStatus = "needs_reconciliation";
              rememberRun();
              throw new Error("工程の安全な中断を確認できませんでした。再送せず実行参照を保持してください。");
            }
            pausedSegment = true;
            progress(receipt);
          }
          if (value.kind === "awaiting_retry") {
            const receipt = parseEventText(value.text);
            if (pausedSegment || retrySegment || sawAnswer || !validRetryBoundary(receipt, runId, conversation)) {
              runStatus = "needs_reconciliation";
              rememberRun();
              throw new Error("契約再試行の安全な中断を確認できませんでした。再送せず実行参照を保持してください。");
            }
            retrySegment = true;
            progress(receipt);
          }
          if (value.kind === "usage") {
            workflow.hidden = false;
            usage.textContent = formatUsage(parseEventText(value.text), value["prompt-tokens"], value["completion-tokens"]);
          }
        }
      }
    }
    if (cancelled) throw new Error("停止しました。下書きを残しています。");
    if (!completed) throw new Error("会話の完了を確認できませんでした。下書きを残しています。");
    if (!pausedSegment && !retrySegment && ["paused", "awaiting_retry"].includes(runStatus)) {
      runStatus = "needs_reconciliation";
      rememberRun();
      throw new Error("安全な中断の終端を確認できませんでした。再送せず実行参照を保持してください。");
    }
    const persisted = await client.call(storeShape, "load", { conversation }, REQUEST_OPTIONS);
    if (retrySegment) {
      if (sawAnswer || runStatus !== "awaiting_retry" || JSON.stringify(persisted.messages) !== JSON.stringify(previous.messages)) {
        runStatus = "needs_reconciliation";
        rememberRun();
        throw new Error("再試行待ちの工程が回答として保存されていないことを確認できませんでした。");
      }
      user.state.textContent = "依頼はチェックポイントに保持されています";
      assistant.state.textContent = "契約不適合の応答を保存して再試行待ち";
      text.value = "";
      nextAction.textContent = "工程はまだ完了していません。「再開」で同じ工程の次の契約修正を明示的に実行します。";
      status.textContent = "応答の契約不適合を保存しました。自動再試行はしていません。";
      contextVerified = canResume = true;
      return;
    }
    if (pausedSegment) {
      if (sawAnswer || runStatus !== "paused" || JSON.stringify(persisted.messages) !== JSON.stringify(previous.messages)) {
        runStatus = "needs_reconciliation";
        rememberRun();
        throw new Error("中断した工程が回答として保存されていないことを確認できませんでした。");
      }
      user.state.textContent = "依頼はチェックポイントに保持されています";
      assistant.state.textContent = "工程を保存して一時停止中";
      text.value = "";
      nextAction.textContent = "「再開」で次の工程を実行します。最終回答はレビュー合格後に表示されます。";
      status.textContent = "1工程を保存しました。続けるには「再開」を押してください。";
      contextVerified = canResume = true;
      return;
    }
    const tail = persisted.messages.slice(-2);
    if (persisted.messages.length < previous.messages.length + 2 || tail[0]?.role !== "user" || (!continuation && tail[0]?.content !== input) || tail[1]?.role !== "assistant" || tail[1]?.content !== assistant.body.textContent) {
      throw new Error("会話の保存を確認できませんでした。下書きを残しています。");
    }
    user.state.textContent = "✓ 保存済み";
    assistant.state.textContent = "✓ 保存済み";
    text.value = "";
    contextVerified = true;
    canAnswer = runStatus === "needs_input";
    status.textContent = runStatus === "needs_input" ? "確認事項への回答を入力してください。途中の成果は保存済みです。" : "準備できました";
  } catch (error) {
    contextVerified = canResume = canAnswer = false;
    referenceBlocked = !!runId;
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
    controls();
  }
}
async function sendMessage(event) {
  event.preventDefault();
  if (!text.value.trim() || send.disabled) return;
  await submit(text.value, runStatus === "needs_input" && !!runId);
}
async function readReference(event) {
  event.preventDefault();
  await restoreReference();
}
async function entryKey(event) {
  const actionButton = event.target === send || event.target === restoreButton;
  if (event.key === "Enter" && actionButton) event.preventDefault();
  const restoreEntry = event.target === restoreRun || event.target === restoreConversation || event.target === restoreButton;
  const composeEntry = event.target === send || (event.target === text && (event.ctrlKey || event.metaKey));
  if (event.key !== "Enter" || event.altKey || event.shiftKey || (!restoreEntry && !composeEntry)) return;
  if (event.isComposing || event.keyCode === 229) {
    return;
  }
  event.preventDefault();
  if (event.repeat) return;
  if (restoreEntry) await readReference(event);
  else await sendMessage(event);
}
// Sandboxed Pack frames intentionally lack allow-forms. Click and explicit
// keyboard actions must not depend on the browser's form submission algorithm.
form.addEventListener("submit", (event) => event.preventDefault());
restoreForm.addEventListener("submit", (event) => event.preventDefault());
form.addEventListener("keydown", entryKey);
restoreForm.addEventListener("keydown", entryKey);
send.addEventListener("click", sendMessage);
restoreButton.addEventListener("click", readReference);
resume.addEventListener("click", () => submit(null, true));
document.querySelector("#new-request").addEventListener("click", () => {
  if (busy) return;
  runId = undefined;
  runStatus = undefined;
  ++restoreGeneration;
  contextVerified = canResume = canAnswer = referenceBlocked = false;
  restoreRun.value = "";
  restoreConversation.value = conversation;
  rememberRun();
  resume.disabled = true;
  workflow.hidden = true;
  status.textContent = "新しい依頼を入力できます。以前の成果はCoreに保存されています。";
  controls();
});
stop.addEventListener("click", async () => {
  cancelled = true;
  contextVerified = canResume = canAnswer = false;
  referenceBlocked = !!runId;
  controls();
  if (active) await active.cancel();
  active = undefined;
  status.textContent = "停止しました";
});
connectPackUi().then((connection) => {
  client = connection;
  controls();
  status.textContent = runId ? "保存参照は未検証です。「保存状態を読む」で確認してください。自動照会・自動再開はしていません。" : "準備できました。保存済みのRunは参照を手動入力して照会できます。";
});
controls();
