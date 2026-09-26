(() => {
  "use strict";

  const contracts = {
    company: "rumi.resource.team.v1",
    runtime: "rumi.resource.team.runtime.v1",
  };
  const nonce = new URLSearchParams(location.hash.slice(1)).get("rumi_rpc_nonce");
  const profileId = new URLSearchParams(location.search).get("profile_id") || "";
  const pending = new Map();
  const byId = (id) => document.getElementById(id);

  addEventListener("message", (event) => {
    if (event.source !== parent || event.origin !== location.origin) return;
    const response = event.data;
    if (!response || response.type !== "rumi.capability.response" || response.nonce !== nonce) return;
    const request = pending.get(response.requestId);
    if (!request) return;
    pending.delete(response.requestId);
    clearTimeout(request.timer);
    response.ok
      ? request.resolve(response.value)
      : request.reject(new Error(response.error || "capability_unavailable"));
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
      type: "rumi.capability.request",
      requestId,
      nonce,
      contractId,
      payload: { operation, input },
    }, location.origin);
  });

  const activeTasks = (company) => Object.values(company?.tasks || {})
    .filter((task) => ["assigned", "running", "waiting"].includes(task?.status));

  const card = (company) => {
    const article = document.createElement("article");
    const title = document.createElement("h3");
    title.textContent = company.name || company.id || "Company";
    const status = document.createElement("span");
    status.className = "status";
    status.textContent = company.status || "unknown";
    const members = document.createElement("span");
    members.className = "meta";
    members.textContent = `Members: ${Object.keys(company.members || {}).length}`;
    const tasks = document.createElement("span");
    tasks.className = "meta";
    tasks.textContent = `Active tasks: ${activeTasks(company).length}`;
    article.append(title, status, members, tasks);
    return article;
  };

  const render = (snapshot, runtime) => {
    const companies = Array.isArray(snapshot?.companies) ? snapshot.companies : [];
    const active = companies.flatMap(activeTasks);
    const runtimeTasks = Array.isArray(runtime?.active_task_ids)
      ? runtime.active_task_ids
      : [];
    byId("company-count").textContent = String(companies.length);
    byId("task-count").textContent = String(active.length);
    byId("runtime-count").textContent = String(runtimeTasks.length);
    const root = byId("companies");
    root.replaceChildren();
    byId("empty").hidden = companies.length !== 0;
    companies.forEach((company) => root.append(card(company)));
  };

  const load = async () => {
    byId("reload").disabled = true;
    byId("error").textContent = "";
    try {
      const [snapshot, runtime] = await Promise.all([
        invoke(contracts.company, "list", { profile_id: profileId }),
        invoke(contracts.runtime, "status", { profile_id: profileId }),
      ]);
      render(snapshot, runtime);
    } catch (reason) {
      byId("error").textContent = reason instanceof Error ? reason.message : String(reason);
    } finally {
      byId("reload").disabled = false;
    }
  };

  byId("profile").textContent = `Profile: ${profileId}`;
  byId("reload").onclick = () => void load();
  void load();
})();

