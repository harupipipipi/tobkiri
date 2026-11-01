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
  const emptyHash = "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855";
  const pending = new Map();
  let studio = null;
  let selected = null;
  const byId = (id) => document.getElementById(id);

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

  const setBusy = (busy) => document.querySelectorAll("button").forEach((button) => { button.disabled = busy; });
  const fail = (reason) => { byId("error").textContent = reason instanceof Error ? reason.message : String(reason); };
  const show = (value) => { byId("output").textContent = JSON.stringify(value, null, 2); };
  const currentPrompt = () => studio?.prompts?.find((item) => item.prompt_id === selected) || null;

  const select = (prompt) => {
    selected = prompt?.prompt_id || "";
    byId("prompt-id").value = selected;
    byId("body").value = prompt?.body || "";
    document.querySelectorAll("nav button[data-id]").forEach((button) => {
      if (button.dataset.id === selected) button.setAttribute("aria-current", "page");
      else button.removeAttribute("aria-current");
    });
  };

  const renderPrompts = () => {
    const nav = byId("prompts"); nav.replaceChildren();
    (studio?.prompts || []).forEach((prompt) => {
      const button = document.createElement("button");
      button.type = "button"; button.dataset.id = prompt.prompt_id;
      button.textContent = `${prompt.prompt_id}${prompt.enabled === false ? " (disabled)" : ""}`;
      button.onclick = () => select(prompt); nav.append(button);
    });
    const create = document.createElement("button"); create.type = "button"; create.textContent = "New prompt";
    create.onclick = () => select({ prompt_id: "new.prompt", body: "", body_hash: emptyHash }); nav.append(create);
  };

  const load = async (promptId = selected || "") => {
    studio = await invoke(contracts.resource, "editor.load", { profile_id: profileId, prompt_id: promptId });
    renderPrompts();
    select(studio.prompts?.find((item) => item.prompt_id === promptId) || studio.selected_prompt || studio.prompts?.[0]);
  };

  const perform = async (contractId, operation, extra = {}, reload = false) => {
    setBusy(true); byId("error").textContent = "";
    try {
      const prompt = currentPrompt();
      const value = await invoke(contractId, operation, {
        profile_id: profileId, prompt_id: byId("prompt-id").value,
        body: byId("body").value, expected_body_hash: prompt?.body_hash || emptyHash, ...extra,
      });
      show(value); if (reload) await load(byId("prompt-id").value); return value;
    } catch (reason) { fail(reason); return null; } finally { setBusy(false); }
  };

  const renderVersions = (versions) => {
    const root = byId("versions"); root.replaceChildren();
    if (!versions.length) return;
    const title = document.createElement("h2"); title.textContent = "Versions"; root.append(title);
    versions.forEach((version) => {
      const article = document.createElement("article"); const label = document.createElement("span");
      label.textContent = `${version.created_at} · ${version.reason}`; const button = document.createElement("button");
      button.type = "button"; button.textContent = "Roll back";
      button.onclick = () => void perform(contracts.version, "rollback", { version_id: version.version_id, use_previous: true }, true);
      article.append(label, button); root.append(article);
    });
  };

  byId("profile").textContent = `Profile: ${profileId}`;
  byId("reload").onclick = () => void load().catch(fail);
  document.querySelectorAll("[data-action]").forEach((button) => {
    button.onclick = async () => {
      const action = button.dataset.action;
      if (action === "versions") {
        const value = await perform(contracts.version, action); renderVersions(value?.versions || []); return;
      }
      const contract = action === "test" ? contracts.test : contracts.author;
      const extra = action === "test" ? { variables: {} } : action === "toggle" ? { enabled: currentPrompt()?.enabled === false } : {};
      await perform(contract, action, extra, action === "save" || action === "toggle");
    };
  });
  void load().catch(fail);
})();
