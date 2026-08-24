const {
  DraftVault,
  HistoryStateStorage,
  LoadSequencer,
  contextIdentity,
  isDirtyDraft,
  preserveEditorState,
  shouldBlockUnload,
  transitionNeedsResolution,
} = globalThis.PromptStudioDraftState;

const contracts = {
  resource: "rumi.resource.prompt.studio.v1",
  author: "rumi.action.prompt.author.v1",
  version: "rumi.action.prompt.version.v1",
  test: "rumi.action.prompt.test.v1",
};
const params = () => new URLSearchParams(location.search);
const nonce = new URLSearchParams(location.hash.slice(1)).get("rumi_rpc_nonce");
const emptyHash = "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855";
const pendingRpc = new Map();
const vault = new DraftVault(new HistoryStateStorage(window.history));
const loads = new LoadSequencer();
const byId = (id) => document.getElementById(id);
const bodyInput = byId("body");
const promptIdInput = byId("prompt-id");

let studio = null;
let selected = "";
let basePrompt = null;
let baseBody = "";
let pendingTransition = null;
let recoveryRecord = null;
let rollbackVersion = null;
let conflictState = null;
let suppressUnloadGuard = false;
let stableUrl = location.href;

const profileId = () => params().get("profile_id") || studio?.profile_id || "";
const modelProfileId = () => params().get("model_profile_id") || params().get("model") || "";
const conversationId = () => params().get("conversation_id") || "";
const currentBaseRevision = () => basePrompt?.body_hash || emptyHash;
const editorContext = () => contextIdentity({
  profileId: profileId(),
  promptId: selected || promptIdInput.value,
  modelProfileId: modelProfileId(),
  conversationId: conversationId(),
  baseRevision: currentBaseRevision(),
});
const requestedContext = (promptId) => contextIdentity({
  profileId: profileId(),
  promptId,
  modelProfileId: modelProfileId(),
  conversationId: conversationId(),
  baseRevision: `load:${promptId || "default"}`,
});
const currentPrompt = () => basePrompt;
const isDirty = () => (
  promptIdInput.value !== selected || isDirtyDraft(baseBody, bodyInput.value)
);
const isConflict = (error) => (
  error?.code === "PROMPT_WRITE_CONFLICT"
  || /PROMPT_WRITE_CONFLICT|stale prompt revision|expected_body_hash/i.test(error?.message || "")
);

addEventListener("message", (event) => {
  if (event.source !== parent || event.origin !== location.origin) return;
  const response = event.data;
  if (!response || response.type !== "rumi.capability.response" || response.nonce !== nonce) return;
  const request = pendingRpc.get(response.requestId);
  if (!request) return;
  pendingRpc.delete(response.requestId);
  clearTimeout(request.timer);
  if (response.ok) request.resolve(response.value);
  else {
    const error = new Error(response.error || "capability_unavailable");
    error.code = response.errorCode || "";
    request.reject(error);
  }
});

const invoke = (contractId, operation, input) => new Promise((resolve, reject) => {
  if (!nonce) return reject(new Error("host RPC session is missing"));
  const requestId = crypto.randomUUID();
  const timer = setTimeout(() => {
    pendingRpc.delete(requestId);
    reject(new Error("capability_timeout"));
  }, 15000);
  pendingRpc.set(requestId, { resolve, reject, timer });
  parent.postMessage({
    type: "rumi.capability.request", requestId, nonce, contractId,
    payload: { operation, input },
  }, location.origin);
});

const setBusy = (busy) => document.querySelectorAll("button").forEach((button) => {
  button.disabled = busy;
});
const fail = (reason, target = "error") => {
  byId(target).textContent = reason instanceof Error ? reason.message : String(reason);
};
const clearError = (target = "error") => { byId(target).textContent = ""; };
const show = (value) => { byId("output").textContent = JSON.stringify(value, null, 2); };
const identityText = (context = editorContext()) => [
  `Prompt: ${context.promptId || "new prompt"}`,
  `Profile: ${context.profileId || "unavailable"}`,
  `Model: ${context.modelProfileId || "default"}`,
].join(" · ");

const captureUiState = () => preserveEditorState({
  selectionStart: bodyInput.selectionStart,
  selectionEnd: bodyInput.selectionEnd,
  editorScrollTop: bodyInput.scrollTop,
  pageScrollX: scrollX,
  pageScrollY: scrollY,
  output: byId("output").textContent,
  outputScrollTop: byId("output").scrollTop,
  versionsExpanded: byId("versions").childElementCount > 0,
});

const restoreUiState = (state = {}) => requestAnimationFrame(() => {
  const start = Math.min(Number(state.selectionStart ?? 0), bodyInput.value.length);
  const end = Math.min(Number(state.selectionEnd ?? start), bodyInput.value.length);
  bodyInput.setSelectionRange(start, end);
  bodyInput.scrollTop = Number(state.editorScrollTop ?? 0);
  byId("output").textContent = String(state.output ?? byId("output").textContent ?? "");
  byId("output").scrollTop = Number(state.outputScrollTop ?? 0);
  scrollTo(Number(state.pageScrollX ?? 0), Number(state.pageScrollY ?? 0));
  bodyInput.focus();
});

const updateDirtyPresentation = () => {
  const dirty = isDirty();
  document.title = `${dirty ? "• " : ""}Prompt Studio`;
  byId("context").textContent = `${identityText()}${dirty ? " · Unsaved changes" : ""}`;
  byId("context").classList.toggle("dirty-indicator", dirty);
  if (nonce) {
    parent.postMessage({ type: "rumi.editor.dirty-state", nonce, dirty }, location.origin);
  }
};

const navigateHost = (href) => {
  if (!nonce) return;
  parent.postMessage({ type: "rumi.navigation.request", nonce, href }, location.origin);
};

const bindDialogChoices = (dialogId) => {
  const dialog = byId(dialogId);
  dialog.querySelectorAll("[data-dialog-value]").forEach((button) => {
    button.onclick = () => dialog.close(button.dataset.dialogValue || "cancel");
  });
};

for (const dialogId of [
  "unsaved-dialog",
  "recovery-dialog",
  "rollback-dialog",
  "conflict-dialog",
]) bindDialogChoices(dialogId);

const persistDraft = () => {
  if (isDirty()) vault.save(editorContext(), bodyInput.value, captureUiState());
};

const setPromptUrl = (promptId) => {
  const url = new URL(location.href);
  if (promptId) url.searchParams.set("prompt_id", promptId);
  else url.searchParams.delete("prompt_id");
  history.replaceState({ ...history.state, prompt_id: promptId }, "", `${url.pathname}${url.search}${url.hash}`);
  stableUrl = location.href;
};

const applyPrompt = (prompt, { offerRecovery = true } = {}) => {
  basePrompt = prompt || { prompt_id: "new.prompt", body: "", body_hash: emptyHash };
  selected = basePrompt.prompt_id || "new.prompt";
  baseBody = String(basePrompt.body || "");
  promptIdInput.value = selected;
  bodyInput.value = baseBody;
  setPromptUrl(selected);
  document.querySelectorAll("nav button[data-id]").forEach((button) => {
    if (button.dataset.id === selected) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  });
  updateDirtyPresentation();
  if (!offerRecovery) return;
  recoveryRecord = vault.restore(editorContext());
  if (recoveryRecord && isDirtyDraft(baseBody, recoveryRecord.draftBody)) {
    byId("recovery-identity").textContent = identityText(recoveryRecord.context);
    byId("recovery-dialog").showModal();
  } else if (recoveryRecord) {
    vault.remove(editorContext());
    recoveryRecord = null;
  }
};

const renderPrompts = () => {
  const nav = byId("prompts");
  nav.replaceChildren();
  (studio?.prompts || []).forEach((prompt) => {
    const button = document.createElement("button");
    button.type = "button";
    button.dataset.id = prompt.prompt_id;
    button.textContent = `${prompt.prompt_id}${prompt.enabled === false ? " (disabled)" : ""}`;
    if (prompt.prompt_id === selected) button.setAttribute("aria-current", "page");
    button.onclick = () => requestTransition(
      "prompt",
      { promptId: prompt.prompt_id },
      () => load(prompt.prompt_id),
    );
    nav.append(button);
  });
  const create = document.createElement("button");
  create.type = "button";
  create.textContent = "New prompt";
  create.onclick = () => requestTransition(
    "prompt",
    { promptId: "new.prompt" },
    () => applyPrompt({ prompt_id: "new.prompt", body: "", body_hash: emptyHash }),
  );
  nav.append(create);
};

const load = async (promptId = selected || params().get("prompt_id") || "") => {
  const context = requestedContext(promptId);
  const token = loads.begin(context);
  const value = await invoke(contracts.resource, "editor.load", {
    profile_id: profileId(),
    prompt_id: promptId,
    model_profile_id: modelProfileId(),
    conversation_id: conversationId(),
  });
  if (!loads.accepts(token, context)) return false;
  studio = value;
  renderPrompts();
  const prompt = studio.prompts?.find((item) => item.prompt_id === promptId)
    || studio.selected_prompt
    || studio.prompts?.[0]
    || { prompt_id: "new.prompt", body: "", body_hash: emptyHash };
  applyPrompt(prompt);
  return true;
};

const latestPrompt = async () => {
  const value = await invoke(contracts.resource, "editor.load", {
    profile_id: profileId(), prompt_id: selected,
    model_profile_id: modelProfileId(), conversation_id: conversationId(),
  });
  return value.prompts?.find((item) => item.prompt_id === selected) || null;
};

const updateSavedPrompt = (prompt) => {
  const oldContext = editorContext();
  vault.remove(oldContext);
  basePrompt = prompt || {
    ...basePrompt,
    prompt_id: promptIdInput.value,
    body: bodyInput.value,
    body_hash: currentBaseRevision(),
  };
  selected = basePrompt.prompt_id;
  baseBody = String(basePrompt.body ?? bodyInput.value);
  promptIdInput.value = selected;
  bodyInput.value = baseBody;
  if (studio?.prompts) {
    studio.prompts = [
      ...studio.prompts.filter((item) => item.prompt_id !== selected),
      basePrompt,
    ].sort((left, right) => left.prompt_id.localeCompare(right.prompt_id));
    renderPrompts();
  }
  setPromptUrl(selected);
  updateDirtyPresentation();
};

const saveDraft = async ({ expectedHash = currentBaseRevision(), mode = "direct" } = {}) => {
  const value = await invoke(contracts.author, "save", {
    profile_id: profileId(),
    prompt_id: promptIdInput.value,
    body: bodyInput.value,
    expected_body_hash: expectedHash,
    reason: mode === "override" ? "save_as_override" : "manual_save",
    save_mode: mode,
  });
  updateSavedPrompt(value.prompt);
  show(value);
  return value;
};

const finishPendingTransition = async () => {
  const pending = pendingTransition;
  pendingTransition = null;
  if (pending) await pending.action();
};

const openConflict = (error, state) => {
  conflictState = { ...state, error, draft: bodyInput.value, uiState: captureUiState(), latest: null };
  persistDraft();
  byId("conflict-identity").textContent = identityText();
  byId("conflict-dialog").showModal();
};

const requestTransition = (kind, target, action) => {
  if (!transitionNeedsResolution(kind, isDirty())) {
    void Promise.resolve(action()).catch(fail);
    return;
  }
  persistDraft();
  pendingTransition = { kind, target, action, uiState: captureUiState() };
  clearError("unsaved-error");
  byId("unsaved-identity").textContent = `${identityText()} → ${target.promptId || target.profileId || target.modelProfileId || kind}`;
  const overrideRequired = basePrompt?.editable === false || basePrompt?.read_only === true;
  byId("save-override").hidden = !overrideRequired;
  byId("unsaved-dialog").showModal();
};

const resolveUnsavedSave = async (mode) => {
  const state = pendingTransition?.uiState || captureUiState();
  setBusy(true);
  try {
    await saveDraft({ mode });
    await finishPendingTransition();
  } catch (error) {
    restoreUiState(state);
    if (isConflict(error)) openConflict(error, { kind: "save", mode });
    else {
      fail(error, "unsaved-error");
      byId("unsaved-dialog").showModal();
    }
  } finally {
    setBusy(false);
  }
};

const perform = async (contractId, operation, extra = {}, reloadAfter = false) => {
  const uiState = captureUiState();
  setBusy(true);
  clearError();
  try {
    const value = await invoke(contractId, operation, {
      profile_id: profileId(),
      prompt_id: promptIdInput.value,
      body: bodyInput.value,
      expected_body_hash: currentBaseRevision(),
      ...extra,
    });
    show(value);
    if (reloadAfter) await load(promptIdInput.value);
    return value;
  } catch (error) {
    restoreUiState(uiState);
    if (isConflict(error)) openConflict(error, { kind: operation });
    else fail(error);
    return null;
  } finally {
    setBusy(false);
  }
};

const renderVersions = (versions) => {
  const root = byId("versions");
  root.replaceChildren();
  if (!versions.length) return;
  const title = document.createElement("h2");
  title.textContent = "Versions";
  root.append(title);
  versions.forEach((version) => {
    const article = document.createElement("article");
    const label = document.createElement("span");
    label.textContent = `${version.created_at} · ${version.reason}`;
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = "Review rollback";
    button.onclick = () => requestTransition(
      "rollback",
      { promptId: selected },
      () => openRollbackReview(version),
    );
    article.append(label, button);
    root.append(article);
  });
};

const openRollbackReview = (version) => {
  rollbackVersion = version;
  clearError("rollback-error");
  byId("rollback-consequence").textContent = [
    `Prompt: ${selected}`,
    `Profile: ${profileId()}`,
    `Current revision: ${currentBaseRevision()}`,
    `Target version: ${version.version_id}`,
    `Target previous revision: ${version.previous_hash || "removed override"}`,
    `Created: ${version.created_at}`,
    `Reason: ${version.reason}`,
  ].join("\n");
  byId("rollback-dialog").showModal();
};

const performRollback = async (expectedHash = currentBaseRevision()) => {
  if (!rollbackVersion) return;
  const uiState = captureUiState();
  setBusy(true);
  try {
    const value = await invoke(contracts.version, "rollback", {
      profile_id: profileId(), prompt_id: selected,
      version_id: rollbackVersion.version_id,
      expected_body_hash: expectedHash, use_previous: true,
    });
    show(value);
    rollbackVersion = null;
    await load(selected);
  } catch (error) {
    restoreUiState(uiState);
    if (isConflict(error)) openConflict(error, { kind: "rollback", version: rollbackVersion });
    else {
      fail(error, "rollback-error");
      byId("rollback-dialog").showModal();
    }
  } finally {
    setBusy(false);
  }
};

byId("unsaved-dialog").addEventListener("close", () => {
  const choice = byId("unsaved-dialog").returnValue;
  if (!choice || choice === "cancel") {
    const state = pendingTransition?.uiState;
    pendingTransition = null;
    if (state) restoreUiState(state);
  } else if (choice === "discard") {
    vault.remove(editorContext());
    bodyInput.value = baseBody;
    promptIdInput.value = selected;
    updateDirtyPresentation();
    void finishPendingTransition();
  } else if (choice === "save" || choice === "override") {
    void resolveUnsavedSave(choice === "override" ? "override" : "direct");
  }
});

byId("recovery-dialog").addEventListener("close", () => {
  if (!recoveryRecord) return;
  if (byId("recovery-dialog").returnValue === "restore") {
    bodyInput.value = recoveryRecord.draftBody;
    updateDirtyPresentation();
    restoreUiState(recoveryRecord.uiState);
  } else if (byId("recovery-dialog").returnValue === "discard") {
    vault.remove(recoveryRecord.context);
  }
  recoveryRecord = null;
});

byId("rollback-dialog").addEventListener("close", () => {
  if (byId("rollback-dialog").returnValue === "rollback") void performRollback();
  else rollbackVersion = null;
});

byId("conflict-dialog").addEventListener("close", async () => {
  const choice = byId("conflict-dialog").returnValue;
  if (!conflictState) return;
  const state = conflictState;
  if (!choice || choice === "cancel") {
    conflictState = null;
    pendingTransition = null;
    restoreUiState(state.uiState);
    return;
  }
  setBusy(true);
  try {
    state.latest ||= await latestPrompt();
    const latestHash = state.latest?.body_hash || emptyHash;
    if (choice === "compare") {
      const value = await invoke(contracts.author, "diff", {
        profile_id: profileId(), prompt_id: selected,
        base: state.latest?.body || "", draft: state.draft,
      });
      show(value);
      restoreUiState({ ...state.uiState, output: JSON.stringify(value, null, 2) });
      byId("conflict-dialog").showModal();
      return;
    }
    if (choice === "reload") {
      vault.remove(editorContext());
      applyPrompt(state.latest || { prompt_id: selected, body: "", body_hash: emptyHash }, { offerRecovery: false });
      conflictState = null;
      await finishPendingTransition();
      return;
    }
    if (choice === "overwrite") {
      bodyInput.value = state.draft;
      if (state.kind === "rollback") {
        rollbackVersion = state.version;
        conflictState = null;
        await performRollback(latestHash);
      } else {
        await saveDraft({ expectedHash: latestHash, mode: state.mode || "override" });
        conflictState = null;
        await finishPendingTransition();
      }
    }
  } catch (error) {
    fail(error);
    restoreUiState(state.uiState);
    conflictState = null;
  } finally {
    setBusy(false);
  }
});

bodyInput.addEventListener("input", () => {
  updateDirtyPresentation();
  persistDraft();
});
promptIdInput.addEventListener("input", () => {
  updateDirtyPresentation();
  persistDraft();
});

byId("reload").onclick = () => requestTransition(
  "refresh",
  { promptId: selected },
  () => load(selected).catch(fail),
);
byId("back").onclick = () => requestTransition("back", { route: "/" }, () => {
  suppressUnloadGuard = true;
  navigateHost("/");
});

document.querySelectorAll("[data-action]").forEach((button) => {
  button.onclick = async () => {
    const action = button.dataset.action;
    if (action === "save") {
      const uiState = captureUiState();
      setBusy(true);
      clearError();
      try { await saveDraft(); }
      catch (error) {
        restoreUiState(uiState);
        if (isConflict(error)) openConflict(error, { kind: "save", mode: "direct" });
        else fail(error);
      } finally { setBusy(false); }
      return;
    }
    if (action === "versions") {
      const value = await perform(contracts.version, action);
      renderVersions(value?.versions || []);
      return;
    }
    const contract = action === "test" ? contracts.test : contracts.author;
    const extra = action === "test"
      ? { variables: {} }
      : action === "toggle"
        ? { enabled: currentPrompt()?.enabled === false }
        : {};
    await perform(contract, action, extra, action === "toggle");
  };
});

addEventListener("beforeunload", (event) => {
  if (!shouldBlockUnload(isDirty(), suppressUnloadGuard)) return;
  persistDraft();
  event.preventDefault();
  event.returnValue = "";
});
addEventListener("pagehide", () => persistDraft());
addEventListener("popstate", () => {
  const targetUrl = location.href;
  if (!isDirty()) {
    stableUrl = targetUrl;
    return;
  }
  history.pushState(history.state, "", stableUrl);
  requestTransition("route", { route: targetUrl }, () => {
    suppressUnloadGuard = true;
    const destination = new URL(targetUrl);
    navigateHost(`${destination.pathname}${destination.search}${destination.hash}`);
  });
});
document.addEventListener("click", (event) => {
  const link = event.target.closest?.("a[href]");
  if (!link || !isDirty()) return;
  const targetUrl = new URL(link.href, location.href);
  if (targetUrl.href === location.href) return;
  event.preventDefault();
  requestTransition("route", { route: targetUrl.pathname }, () => {
    suppressUnloadGuard = true;
    navigateHost(`${targetUrl.pathname}${targetUrl.search}${targetUrl.hash}`);
  });
});

void load(params().get("prompt_id") || "").catch(fail);
