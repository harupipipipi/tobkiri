(() => {
  "use strict";

  const contracts = {
    resource: "rumi.resource.prompt.studio.v1",
    author: "rumi.action.prompt.author.v1",
    version: "rumi.action.prompt.version.v1",
    test: "rumi.action.prompt.test.v1",
  };
  const nonce = new URLSearchParams(location.hash.slice(1)).get("rumi_rpc_nonce");
  const profileId = new URLSearchParams(location.search).get("profile_id") || "";
  const requestedLocale = new URLSearchParams(location.search).get("locale") || "";
  const emptyHash = "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855";
  const pending = new Map();
  const byId = (id) => document.getElementById(id);
  const promptIdPattern = /^[A-Za-z0-9_.-]+$/;

  const messages = {
    en: {
      profile: "Profile: {profile}", title: "Prompt Studio", language: "Language",
      reload: "Reload Prompt Studio", errorTitle: "Prompt Studio could not complete the action",
      dismissError: "Dismiss error", prompts: "Prompts", search: "Search prompts",
      searchHelp: "Searches prompt IDs and descriptions. The selected prompt remains open when filtered out.",
      filter: "Filter prompts", all: "All", active: "Active", editable: "Editable",
      readonly: "Read-only", overrides: "Overrides",
      listHelp: "Use Up, Down, Home, and End to move through prompts. Press Enter or Space to select.",
      newPrompt: "Create a new prompt", editor: "Prompt editor", noSelection: "No prompt selected.",
      promptId: "Prompt ID", promptIdHelp: "Existing prompt IDs are immutable. Create a new prompt to choose a new ID.",
      promptBody: "Prompt body", promptBodyHelp: "Prompt text is passive and cannot grant permissions, attach tools, or change models.",
      actions: "Actions for the selected prompt", inspector: "Inspector", inspectorLabel: "Prompt inspector",
      statusTab: "Status", resultTab: "Operation result", versionsTab: "Versions",
      stateHeading: "Prompt state summary", selection: "Selection", activation: "Activation",
      editing: "Editing", draft: "Draft", tokenizer: "Tokenizer", safety: "Safety",
      none: "None", unavailable: "Unavailable", saved: "Saved", unsaved: "Unsaved changes",
      tokenizerUnknown: "Not reported by this local pack",
      safetyValue: "Brokered local execution; no network or credential access",
      resultHeading: "Latest operation result", noResult: "No operation has run.",
      versionsHeading: "Prompt versions",
      versionsHelp: "Rollback always asks for confirmation and identifies the affected prompt and version.",
      noVersions: "No saved versions are available for this prompt.",
      rollbackTitle: "Confirm prompt rollback", cancel: "Cancel", close: "Close",
      confirmRollback: "Confirm rollback", invalidId: "Use only letters, numbers, periods, underscores, or hyphens in the prompt ID.",
      selectedSummary: "Selected prompt {prompt}. {activation}. {editing}.",
      activationActive: "Active", activationDisabled: "Disabled", editingEditable: "Editable pack-owned prompt",
      editingNew: "New prompt; ID and body are editable", editingReadonly: "Read-only; override is not available in this isolated owner",
      overrideAvailable: "Read-only; an override can be created", overrideState: "Profile override",
      filterCount: "{count} of {total} prompts shown.", filterEmpty: "No prompts match the current search and filter.",
      selectedFiltered: "The selected prompt remains open but is hidden by the current filter.",
      promptOption: "{prompt}. {activation}. {editing}.", newId: "new.prompt",
      save: "Save prompt {prompt}", diff: "Compare draft for prompt {prompt}",
      lint: "Lint prompt {prompt}", compact: "Compact prompt {prompt}",
      test: "Test prompt {prompt} locally without model or tool execution",
      enable: "Enable prompt {prompt}", disable: "Disable prompt {prompt}",
      loadVersions: "Load versions for prompt {prompt}",
      loading: "Loading Prompt Studio…", loaded: "Prompt Studio loaded. {prompt} is selected.",
      reloaded: "Prompt Studio reloaded. Focus was preserved.",
      working: "{action} is in progress.", complete: "{action} completed for prompt {prompt}.",
      failed: "{action} failed: {reason}", selectedAnnounce: "Selected prompt {prompt}.",
      newAnnounce: "New prompt editor opened. Choose a prompt ID.",
      versionLabel: "Version {version}, created {created}, reason {reason}",
      rollbackAction: "Roll back prompt {prompt} to version {version}, created {created}, reason {reason}",
      rollbackDescription: "Roll back prompt {prompt} to version {version}, created {created}? The recorded reason is {reason}. This creates a new audit version and cannot be applied without confirmation.",
      rollbackPending: "Rollback of prompt {prompt} to version {version} is pending.",
      rollbackSettled: "Rollback of prompt {prompt} to version {version} completed.",
      rollbackFailed: "Rollback failed for prompt {prompt}, version {version}: {reason}",
      saveAction: "Save", diffAction: "Diff", lintAction: "Lint", compactAction: "Compact",
      testAction: "Local test", toggleAction: "Activation update", versionsAction: "Version load",
    },
    ja: {
      profile: "プロファイル: {profile}", title: "Prompt Studio", language: "表示言語",
      reload: "Prompt Studio を再読み込み", errorTitle: "Prompt Studio は操作を完了できませんでした",
      dismissError: "エラーを閉じる", prompts: "プロンプト", search: "プロンプトを検索",
      searchHelp: "プロンプト ID と説明を検索します。絞り込みで非表示になっても、選択中のプロンプトは開いたままです。",
      filter: "プロンプトを絞り込む", all: "すべて", active: "有効", editable: "編集可能",
      readonly: "読み取り専用", overrides: "上書き",
      listHelp: "上・下・Home・End キーで移動し、Enter または Space キーで選択します。",
      newPrompt: "新しいプロンプトを作成", editor: "プロンプトエディタ", noSelection: "プロンプトは選択されていません。",
      promptId: "プロンプト ID", promptIdHelp: "既存のプロンプト ID は変更できません。新規作成では新しい ID を指定できます。",
      promptBody: "プロンプト本文", promptBodyHelp: "プロンプト本文は受動的なテキストであり、権限付与、ツール追加、モデル変更はできません。",
      actions: "選択中のプロンプトに対する操作", inspector: "インスペクタ", inspectorLabel: "プロンプトインスペクタ",
      statusTab: "状態", resultTab: "操作結果", versionsTab: "バージョン",
      stateHeading: "プロンプト状態の概要", selection: "選択", activation: "有効化",
      editing: "編集状態", draft: "下書き", tokenizer: "Tokenizer", safety: "安全境界",
      none: "なし", unavailable: "利用不可", saved: "保存済み", unsaved: "未保存の変更あり",
      tokenizerUnknown: "このローカル Pack からは報告されていません",
      safetyValue: "Broker 経由のローカル実行。ネットワークと認証情報へのアクセスなし",
      resultHeading: "直近の操作結果", noResult: "まだ操作は実行されていません。",
      versionsHeading: "プロンプトのバージョン",
      versionsHelp: "ロールバック前に必ず確認し、対象のプロンプトとバージョンを明示します。",
      noVersions: "このプロンプトには保存済みバージョンがありません。",
      rollbackTitle: "プロンプトのロールバックを確認", cancel: "キャンセル", close: "閉じる",
      confirmRollback: "ロールバックを確定", invalidId: "プロンプト ID には英数字、ピリオド、アンダースコア、ハイフンだけを使用してください。",
      selectedSummary: "{prompt} を選択中。{activation}。{editing}。",
      activationActive: "有効", activationDisabled: "無効", editingEditable: "Pack が所有する編集可能なプロンプト",
      editingNew: "新規プロンプト。ID と本文を編集できます", editingReadonly: "読み取り専用。この独立した所有境界では上書きできません",
      overrideAvailable: "読み取り専用。上書きを作成できます", overrideState: "プロファイル上書き",
      filterCount: "全 {total} 件中 {count} 件のプロンプトを表示しています。", filterEmpty: "現在の検索条件と絞り込みに一致するプロンプトはありません。",
      selectedFiltered: "選択中のプロンプトは開いたままですが、現在の絞り込みでは非表示です。",
      promptOption: "{prompt}。{activation}。{editing}。", newId: "new.prompt",
      save: "プロンプト {prompt} を保存", diff: "プロンプト {prompt} の下書きを比較",
      lint: "プロンプト {prompt} を検査", compact: "プロンプト {prompt} を圧縮",
      test: "モデルやツールを実行せずプロンプト {prompt} をローカルテスト",
      enable: "プロンプト {prompt} を有効化", disable: "プロンプト {prompt} を無効化",
      loadVersions: "プロンプト {prompt} のバージョンを読み込む",
      loading: "Prompt Studio を読み込み中…", loaded: "Prompt Studio を読み込みました。{prompt} を選択しています。",
      reloaded: "Prompt Studio を再読み込みしました。フォーカスは維持されています。",
      working: "{action}を実行中です。", complete: "プロンプト {prompt} の{action}が完了しました。",
      failed: "{action}に失敗しました: {reason}", selectedAnnounce: "プロンプト {prompt} を選択しました。",
      newAnnounce: "新規プロンプトエディタを開きました。プロンプト ID を指定してください。",
      versionLabel: "バージョン {version}、作成日時 {created}、理由 {reason}",
      rollbackAction: "プロンプト {prompt} をバージョン {version}（作成日時: {created}、理由: {reason}）へロールバック",
      rollbackDescription: "プロンプト {prompt} を作成日時 {created} のバージョン {version} に戻しますか。記録された理由は {reason} です。確認後にのみ適用され、新しい監査バージョンが作成されます。",
      rollbackPending: "プロンプト {prompt} をバージョン {version} へロールバックしています。",
      rollbackSettled: "プロンプト {prompt} のバージョン {version} へのロールバックが完了しました。",
      rollbackFailed: "プロンプト {prompt}、バージョン {version} のロールバックに失敗しました: {reason}",
      saveAction: "保存", diffAction: "差分確認", lintAction: "検査", compactAction: "圧縮",
      testAction: "ローカルテスト", toggleAction: "有効状態の更新", versionsAction: "バージョン読み込み",
    },
  };

  let locale = initialLocale();
  let studio = null;
  let selected = "";
  let newDraft = false;
  let filter = "all";
  let query = "";
  let activeTab = "status";
  let busyAction = "";
  let rollbackTarget = null;
  let rollbackReturnFocus = null;

  function initialLocale() {
    if (requestedLocale === "ja" || requestedLocale === "en") return requestedLocale;
    try {
      const saved = localStorage.getItem("tobkiri.prompt-studio.locale");
      if (saved === "ja" || saved === "en") return saved;
    } catch {}
    return navigator.language.toLowerCase().startsWith("ja") ? "ja" : "en";
  }

  function t(key, values = {}) {
    const template = messages[locale][key] || messages.en[key] || key;
    return template.replace(/\{([a-z]+)\}/gi, (_, name) => String(values[name] ?? ""));
  }

  function safeId(value) {
    return String(value || "prompt").replace(/[^A-Za-z0-9_.-]/g, "-");
  }

  function currentPrompt() {
    if (newDraft) return null;
    return studio?.prompts?.find((item) => item.prompt_id === selected) || null;
  }

  function promptName() {
    return byId("prompt-id").value.trim() || selected || t("newId");
  }

  function promptEditing(prompt) {
    if (newDraft) return t("editingNew");
    if (prompt?.read_only) return t("editingReadonly");
    return t("editingEditable");
  }

  function promptActivation(prompt) {
    return prompt?.enabled === false ? t("activationDisabled") : t("activationActive");
  }

  function isOverride(prompt) {
    return Boolean(prompt?.metadata?.override || prompt?.metadata?.is_override);
  }

  function isDirty() {
    const prompt = currentPrompt();
    if (newDraft) return Boolean(byId("prompt-id").value.trim() || byId("body").value);
    return Boolean(prompt) && byId("body").value !== String(prompt.body || "");
  }

  function announce(message) {
    const status = byId("studio-status");
    status.textContent = "";
    requestAnimationFrame(() => { status.textContent = message; });
  }

  function clearError() {
    byId("error").textContent = "";
    byId("error-region").hidden = true;
  }

  function fail(reason, action = "Prompt Studio", focus = true) {
    const detail = reason instanceof Error ? reason.message : String(reason);
    byId("error").textContent = t("failed", { action, reason: detail });
    byId("error-region").hidden = false;
    if (focus) byId("error-region").focus({ preventScroll: false });
  }

  function show(value) {
    byId("output").textContent = JSON.stringify(value, null, 2);
    activateTab("result");
  }

  function setBusy(action, label) {
    busyAction = action;
    byId("editor").setAttribute("aria-busy", "true");
    document.querySelectorAll("[data-action], #reload, #new-prompt").forEach((element) => {
      element.disabled = true;
    });
    announce(t("working", { action: label }));
    updateActionState();
  }

  function clearBusy() {
    busyAction = "";
    byId("editor").setAttribute("aria-busy", "false");
    byId("reload").disabled = false;
    byId("new-prompt").disabled = false;
    updateActionState();
  }

  addEventListener("message", (event) => {
    if (event.source !== parent || event.origin !== location.origin) return;
    const response = event.data;
    if (!response || response.type !== "rumi.capability.response" || response.nonce !== nonce) return;
    const request = pending.get(response.requestId);
    if (!request) return;
    pending.delete(response.requestId);
    clearTimeout(request.timer);
    response.ok ? request.resolve(response.value) : request.reject(new Error(response.error || "capability_unavailable"));
  });

  const invoke = (contractId, operation, input) => new Promise((resolve, reject) => {
    if (!nonce) return reject(new Error("host RPC session is missing"));
    const requestId = crypto.randomUUID();
    const timer = setTimeout(() => {
      pending.delete(requestId);
      reject(new Error("capability_timeout"));
    }, 15000);
    pending.set(requestId, { resolve, reject, timer });
    parent.postMessage({
      type: "rumi.capability.request", requestId, nonce, contractId,
      payload: { operation, input },
    }, location.origin);
  });

  function visiblePrompts() {
    const normalizedQuery = query.trim().toLowerCase();
    return (studio?.prompts || []).filter((prompt) => {
      if (filter === "active" && prompt.enabled === false) return false;
      if (filter === "readonly" && !prompt.read_only) return false;
      if (filter === "editable" && prompt.read_only) return false;
      if (filter === "overrides" && !isOverride(prompt)) return false;
      if (!normalizedQuery) return true;
      return [prompt.prompt_id, prompt.description, prompt.body]
        .some((value) => String(value || "").toLowerCase().includes(normalizedQuery));
    });
  }

  function focusOption(index) {
    const options = [...byId("prompts").querySelectorAll("[role=option]")];
    if (!options.length) return;
    const next = Math.max(0, Math.min(index, options.length - 1));
    options.forEach((option, optionIndex) => { option.tabIndex = optionIndex === next ? 0 : -1; });
    options[next].focus();
  }

  function optionKeydown(event) {
    const options = [...byId("prompts").querySelectorAll("[role=option]")];
    const index = options.indexOf(event.currentTarget);
    if (["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) {
      event.preventDefault();
      const next = event.key === "Home" ? 0 : event.key === "End" ? options.length - 1 : index + (event.key === "ArrowDown" ? 1 : -1);
      focusOption(next);
      return;
    }
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      const prompt = (studio?.prompts || []).find((item) => item.prompt_id === event.currentTarget.dataset.id);
      if (prompt) selectPrompt(prompt, { announceSelection: true, focusOption: true });
    }
  }

  function renderPrompts({ announceCount = false, focusPromptId = "" } = {}) {
    const root = byId("prompts");
    root.replaceChildren();
    const prompts = visiblePrompts();
    const selectedVisible = prompts.some((prompt) => prompt.prompt_id === selected);
    prompts.forEach((prompt, index) => {
      const option = document.createElement("div");
      option.id = `prompt-option-${safeId(prompt.prompt_id)}`;
      option.className = "prompt-option";
      option.dataset.id = prompt.prompt_id;
      option.setAttribute("role", "option");
      option.setAttribute("aria-selected", String(prompt.prompt_id === selected));
      option.setAttribute("aria-label", t("promptOption", {
        prompt: prompt.prompt_id,
        activation: promptActivation(prompt),
        editing: promptEditing(prompt),
      }));
      option.tabIndex = prompt.prompt_id === selected || (!selectedVisible && index === 0) ? 0 : -1;

      const name = document.createElement("strong");
      name.textContent = prompt.prompt_id;
      const state = document.createElement("span");
      state.textContent = `${promptActivation(prompt)} · ${promptEditing(prompt)}`;
      option.append(name, state);
      option.addEventListener("click", () => selectPrompt(prompt, { announceSelection: true, focusOption: true }));
      option.addEventListener("keydown", optionKeydown);
      root.append(option);
    });
    if (!prompts.length) {
      const empty = document.createElement("p");
      empty.className = "empty-state";
      empty.setAttribute("role", "none");
      empty.textContent = t("filterEmpty");
      root.append(empty);
    }
    const total = studio?.prompts?.length || 0;
    const selectedFiltered = Boolean(selected && !newDraft && !prompts.some((prompt) => prompt.prompt_id === selected));
    byId("prompt-list-status").textContent = `${t("filterCount", { count: prompts.length, total })}${selectedFiltered ? ` ${t("selectedFiltered")}` : ""}`;
    if (announceCount) announce(byId("prompt-list-status").textContent);
    if (focusPromptId) {
      const target = byId(`prompt-option-${safeId(focusPromptId)}`);
      if (target) target.focus();
    }
  }

  function selectPrompt(prompt, options = {}) {
    newDraft = Boolean(prompt?.__new);
    selected = newDraft ? "" : String(prompt?.prompt_id || "");
    byId("prompt-id").value = String(prompt?.prompt_id || t("newId"));
    byId("prompt-id").readOnly = !newDraft;
    byId("body").value = String(prompt?.body || "");
    byId("body").readOnly = Boolean(prompt?.read_only);
    byId("field-error").textContent = "";
    clearError();
    renderPrompts({ focusPromptId: options.focusOption ? selected : "" });
    activateTab("status");
    updateStateSummary();
    updateActionState();
    if (options.announceSelection) {
      announce(newDraft ? t("newAnnounce") : t("selectedAnnounce", { prompt: selected }));
    }
  }

  function rememberFocus() {
    const active = document.activeElement;
    if (!active || active === document.body) return null;
    if (active.id) return { id: active.id };
    if (active.dataset?.id) return { promptId: active.dataset.id };
    if (active.dataset?.action) return { action: active.dataset.action };
    return null;
  }

  function restoreFocus(snapshot) {
    if (!snapshot) return;
    const target = snapshot.id
      ? byId(snapshot.id)
      : snapshot.promptId
        ? byId(`prompt-option-${safeId(snapshot.promptId)}`)
        : document.querySelector(`[data-action="${snapshot.action}"]`);
    if (target && !target.disabled && !target.hidden) target.focus({ preventScroll: true });
  }

  async function load(promptId = selected || "", options = {}) {
    const focus = options.preserveFocus === false ? null : rememberFocus();
    if (!studio) announce(t("loading"));
    const value = await invoke(contracts.resource, "editor.load", { profile_id: profileId, prompt_id: promptId });
    studio = value;
    const next = studio.prompts?.find((item) => item.prompt_id === promptId) || studio.selected_prompt || studio.prompts?.[0];
    selectPrompt(next || { __new: true, prompt_id: t("newId"), body: "", body_hash: emptyHash });
    restoreFocus(focus);
    announce(options.reload ? t("reloaded") : t("loaded", { prompt: promptName() }));
  }

  function validatePromptId() {
    const value = byId("prompt-id").value.trim();
    const message = value && promptIdPattern.test(value) ? "" : t("invalidId");
    byId("field-error").textContent = message;
    byId("prompt-id").setAttribute("aria-invalid", String(Boolean(message)));
    return !message;
  }

  function actionLabel(action) {
    return t(`${action}Action`);
  }

  async function perform(contractId, operation, extra = {}, reloadAfter = false) {
    const action = operation === "toggle" ? "toggle" : operation;
    const label = actionLabel(action);
    if (!validatePromptId()) {
      byId("prompt-id").focus();
      return null;
    }
    const focus = rememberFocus();
    let completed = false;
    setBusy(action, label);
    clearError();
    try {
      const prompt = currentPrompt();
      const value = await invoke(contractId, operation, {
        profile_id: profileId,
        prompt_id: byId("prompt-id").value.trim(),
        body: byId("body").value,
        expected_body_hash: prompt?.body_hash || emptyHash,
        ...extra,
      });
      show(value);
      if (reloadAfter) {
        await load(byId("prompt-id").value.trim(), { preserveFocus: false });
      }
      announce(t("complete", { action: label, prompt: promptName() }));
      completed = true;
      return value;
    } catch (reason) {
      fail(reason, label);
      return null;
    } finally {
      clearBusy();
      if (completed) restoreFocus(focus);
    }
  }

  function renderVersions(versions) {
    const root = byId("versions");
    root.replaceChildren();
    if (!versions.length) {
      const empty = document.createElement("p");
      empty.className = "empty-state";
      empty.textContent = t("noVersions");
      root.append(empty);
      return;
    }
    versions.forEach((version) => {
      const reason = String(version.reason || "unspecified");
      const created = String(version.created_at || "unknown");
      const versionId = String(version.version_id || "unknown");
      const article = document.createElement("article");
      const label = document.createElement("span");
      label.textContent = t("versionLabel", { version: versionId, created, reason });
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = t("rollbackAction", {
        prompt: promptName(), version: versionId, created, reason,
      });
      button.setAttribute("aria-label", button.textContent);
      button.addEventListener("click", () => requestRollback({ versionId, reason, created }, button));
      article.append(label, button);
      root.append(article);
    });
  }

  function requestRollback(version, returnFocus) {
    rollbackTarget = { ...version, promptId: promptName() };
    rollbackReturnFocus = returnFocus;
    const dialog = byId("rollback-dialog");
    dialog.setAttribute("aria-busy", "false");
    byId("rollback-description").textContent = t("rollbackDescription", {
      prompt: rollbackTarget.promptId,
      version: rollbackTarget.versionId,
      created: rollbackTarget.created,
      reason: rollbackTarget.reason,
    });
    byId("rollback-status").textContent = "";
    byId("rollback-status").setAttribute("role", "status");
    byId("confirm-rollback").hidden = false;
    byId("confirm-rollback").disabled = false;
    byId("confirm-rollback").textContent = t("confirmRollback");
    byId("confirm-rollback").setAttribute("aria-label", t("rollbackAction", {
      prompt: rollbackTarget.promptId,
      version: rollbackTarget.versionId,
      created: rollbackTarget.created,
      reason: rollbackTarget.reason,
    }));
    byId("cancel-rollback").textContent = t("cancel");
    byId("cancel-rollback").disabled = false;
    dialog.showModal();
    byId("cancel-rollback").focus();
  }

  async function confirmRollback() {
    if (!rollbackTarget || busyAction === "rollback") return;
    const target = { ...rollbackTarget };
    const prompt = currentPrompt();
    busyAction = "rollback";
    const dialog = byId("rollback-dialog");
    dialog.setAttribute("aria-busy", "true");
    byId("confirm-rollback").disabled = true;
    byId("cancel-rollback").disabled = true;
    byId("rollback-status").setAttribute("role", "status");
    byId("rollback-status").textContent = t("rollbackPending", { prompt: target.promptId, version: target.versionId });
    try {
      const value = await invoke(contracts.version, "rollback", {
        profile_id: profileId,
        prompt_id: target.promptId,
        expected_body_hash: prompt?.body_hash || emptyHash,
        version_id: target.versionId,
        use_previous: true,
      });
      show(value);
      await load(target.promptId, { preserveFocus: false });
      const versionsValue = await invoke(contracts.version, "versions", { profile_id: profileId, prompt_id: target.promptId });
      renderVersions(versionsValue?.versions || []);
      activateTab("versions");
      byId("rollback-status").textContent = t("rollbackSettled", { prompt: target.promptId, version: target.versionId });
      byId("confirm-rollback").hidden = true;
      byId("cancel-rollback").textContent = t("close");
      byId("cancel-rollback").disabled = false;
      byId("cancel-rollback").focus();
    } catch (reason) {
      const detail = reason instanceof Error ? reason.message : String(reason);
      byId("rollback-status").setAttribute("role", "alert");
      byId("rollback-status").textContent = t("rollbackFailed", { prompt: target.promptId, version: target.versionId, reason: detail });
      byId("confirm-rollback").disabled = false;
      byId("cancel-rollback").disabled = false;
      byId("confirm-rollback").focus();
    } finally {
      dialog.setAttribute("aria-busy", "false");
      busyAction = "";
    }
  }

  function closeRollback() {
    if (busyAction === "rollback") return;
    byId("rollback-dialog").close();
  }

  function activateTab(tab, options = {}) {
    activeTab = tab;
    document.querySelectorAll("[role=tab]").forEach((button) => {
      const selectedTab = button.dataset.tab === tab;
      button.setAttribute("aria-selected", String(selectedTab));
      button.tabIndex = selectedTab ? 0 : -1;
      if (selectedTab && options.focus) button.focus();
    });
    document.querySelectorAll("[role=tabpanel]").forEach((panel) => {
      panel.hidden = panel.dataset.panel !== tab;
    });
  }

  function tabKeydown(event) {
    const tabs = [...byId("inspector-tabs").querySelectorAll("[role=tab]")];
    const index = tabs.indexOf(event.currentTarget);
    let next = null;
    if (event.key === "ArrowRight") next = (index + 1) % tabs.length;
    if (event.key === "ArrowLeft") next = (index - 1 + tabs.length) % tabs.length;
    if (event.key === "Home") next = 0;
    if (event.key === "End") next = tabs.length - 1;
    if (next === null) return;
    event.preventDefault();
    activateTab(tabs[next].dataset.tab, { focus: true });
  }

  function updateStateSummary() {
    const prompt = currentPrompt();
    const name = promptName();
    const activation = newDraft ? t("unavailable") : promptActivation(prompt);
    const editing = promptEditing(prompt);
    const dirty = isDirty() ? t("unsaved") : t("saved");
    byId("selection-summary").textContent = newDraft
      ? t("newAnnounce")
      : t("selectedSummary", { prompt: name, activation, editing });
    byId("dirty-badge").textContent = dirty;
    byId("dirty-badge").dataset.dirty = String(isDirty());
    byId("state-selection").textContent = newDraft ? t("newPrompt") : name || t("none");
    byId("state-activation").textContent = activation;
    byId("state-editing").textContent = isOverride(prompt) ? `${editing}; ${t("overrideState")}` : editing;
    byId("state-dirty").textContent = dirty;
    byId("state-tokenizer").textContent = t("tokenizerUnknown");
    byId("state-safety").textContent = t("safetyValue");
  }

  function updateActionState() {
    const prompt = currentPrompt();
    const name = promptName();
    const labels = {
      save: t("save", { prompt: name }), diff: t("diff", { prompt: name }),
      lint: t("lint", { prompt: name }), compact: t("compact", { prompt: name }),
      test: t("test", { prompt: name }),
      toggle: t(prompt?.enabled === false ? "enable" : "disable", { prompt: name }),
      versions: t("loadVersions", { prompt: name }),
    };
    document.querySelectorAll("[data-action]").forEach((button) => {
      const action = button.dataset.action;
      button.textContent = labels[action];
      button.setAttribute("aria-label", labels[action]);
      const needsExisting = action === "toggle" || action === "versions";
      const blockedByReadonly = Boolean(prompt?.read_only) && ["save", "compact"].includes(action);
      button.disabled = Boolean(busyAction) || !name || (needsExisting && !prompt) || blockedByReadonly;
    });
    document.querySelector(".actions").setAttribute("aria-label", t("actions"));
  }

  function applyLocale() {
    document.documentElement.lang = locale;
    document.title = t("title");
    byId("locale").value = locale;
    byId("profile").textContent = t("profile", { profile: profileId || t("unavailable") });
    const textById = {
      "studio-title": "title", "locale-label": "language", reload: "reload",
      "error-title": "errorTitle", "dismiss-error": "dismissError", "prompt-list-heading": "prompts",
      "prompt-search-label": "search", "prompt-search-help": "searchHelp", "prompt-filter-label": "filter",
      "prompt-list-help": "listHelp", "new-prompt": "newPrompt", "editor-heading": "editor",
      "prompt-id-label": "promptId", "prompt-id-help": "promptIdHelp", "prompt-body-label": "promptBody",
      "prompt-body-help": "promptBodyHelp", "inspector-heading": "inspector", "tab-status": "statusTab",
      "tab-result": "resultTab", "tab-versions": "versionsTab", "state-summary-heading": "stateHeading",
      "result-heading": "resultHeading", "versions-heading": "versionsHeading", "versions-help": "versionsHelp",
      "rollback-title": "rollbackTitle", "cancel-rollback": "cancel", "confirm-rollback": "confirmRollback",
    };
    Object.entries(textById).forEach(([id, key]) => { byId(id).textContent = t(key); });
    const filterKeys = { all: "all", active: "active", editable: "editable", readonly: "readonly", overrides: "overrides" };
    document.querySelectorAll("[data-filter]").forEach((button) => { button.textContent = t(filterKeys[button.dataset.filter]); });
    byId("inspector-tabs").setAttribute("aria-label", t("inspectorLabel"));
    const terms = ["selection", "activation", "editing", "draft", "tokenizer", "safety"];
    document.querySelectorAll(".state-summary dt").forEach((term, index) => { term.textContent = t(terms[index]); });
    if (byId("output").textContent === messages.en.noResult || byId("output").textContent === messages.ja.noResult) {
      byId("output").textContent = t("noResult");
    }
    updateStateSummary();
    updateActionState();
    renderPrompts();
  }

  byId("locale").addEventListener("change", () => {
    locale = byId("locale").value === "ja" ? "ja" : "en";
    try { localStorage.setItem("tobkiri.prompt-studio.locale", locale); } catch {}
    applyLocale();
    announce(t("loaded", { prompt: promptName() }));
  });
  byId("reload").addEventListener("click", () => {
    clearError();
    void load(selected, { reload: true }).catch((reason) => fail(reason, t("reload")));
  });
  byId("dismiss-error").addEventListener("click", () => {
    clearError();
    byId("body").focus();
  });
  byId("new-prompt").addEventListener("click", () => {
    selectPrompt({ __new: true, prompt_id: t("newId"), body: "", body_hash: emptyHash }, { announceSelection: true });
    byId("prompt-id").select();
  });
  byId("prompt-search").addEventListener("input", (event) => {
    query = event.currentTarget.value;
    renderPrompts({ announceCount: true });
  });
  document.querySelectorAll("[data-filter]").forEach((button) => {
    button.addEventListener("click", () => {
      filter = button.dataset.filter;
      document.querySelectorAll("[data-filter]").forEach((item) => {
        item.setAttribute("aria-pressed", String(item === button));
      });
      renderPrompts({ announceCount: true });
    });
  });
  [byId("prompt-id"), byId("body")].forEach((field) => {
    field.addEventListener("input", () => {
      if (field === byId("prompt-id")) validatePromptId();
      updateStateSummary();
      updateActionState();
    });
  });
  document.querySelectorAll("[role=tab]").forEach((button) => {
    button.addEventListener("click", () => activateTab(button.dataset.tab));
    button.addEventListener("keydown", tabKeydown);
  });
  document.querySelectorAll("[data-action]").forEach((button) => {
    button.addEventListener("click", async () => {
      const action = button.dataset.action;
      if (action === "versions") {
        const value = await perform(contracts.version, action);
        if (value) {
          renderVersions(value.versions || []);
          activateTab("versions");
        }
        return;
      }
      const contract = action === "test" ? contracts.test : contracts.author;
      const extra = action === "test" ? { variables: {} } : action === "toggle" ? { enabled: currentPrompt()?.enabled === false } : {};
      await perform(contract, action, extra, action === "save" || action === "toggle");
    });
  });
  byId("cancel-rollback").addEventListener("click", closeRollback);
  byId("confirm-rollback").addEventListener("click", () => void confirmRollback());
  byId("rollback-dialog").addEventListener("cancel", (event) => {
    if (busyAction === "rollback") event.preventDefault();
  });
  byId("rollback-dialog").addEventListener("close", () => {
    const target = rollbackReturnFocus;
    rollbackTarget = null;
    rollbackReturnFocus = null;
    if (target?.isConnected && !target.disabled) target.focus();
    else byId("tab-versions").focus();
  });

  applyLocale();
  activateTab(activeTab);
  void load().catch((reason) => fail(reason, t("reload")));
})();
