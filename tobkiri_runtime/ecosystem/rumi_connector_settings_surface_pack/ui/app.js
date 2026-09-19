(() => {
  "use strict";
  const gatewayContract = "rumi.resource.connector.gateway.v1";
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
    response.ok ? request.resolve(response.value) : request.reject(new Error(response.error || "capability_unavailable"));
  });

  const invoke = (contractId, operation, input) => new Promise((resolve, reject) => {
    if (!nonce) return reject(new Error("host RPC session is missing"));
    const requestId = crypto.randomUUID();
    const timer = setTimeout(() => { pending.delete(requestId); reject(new Error("capability_timeout")); }, 15000);
    pending.set(requestId, { resolve, reject, timer });
    parent.postMessage({ type: "rumi.capability.request", requestId, nonce, contractId, payload: { operation, input } }, location.origin);
  });

  const card = (title, status, lines) => {
    const article = document.createElement("article");
    const heading = document.createElement("h3"); heading.textContent = title;
    const badge = document.createElement("span"); badge.className = "status"; badge.textContent = status;
    article.append(heading, badge);
    lines.forEach((line) => { const meta = document.createElement("span"); meta.className = "meta"; meta.textContent = line; article.append(meta); });
    return article;
  };

  const render = (gateway) => {
    const connectors = Array.isArray(gateway?.connectors) ? gateway.connectors : [];
    byId("connector-count").textContent = String(connectors.length);
    byId("enabled-count").textContent = String(connectors.filter((item) => item.enabled).length);
    byId("credential-count").textContent = String(
      connectors.filter((item) => item.credential_configured).length,
    );
    const connectorRoot = byId("connectors"); connectorRoot.replaceChildren();
    byId("empty").hidden = connectors.length !== 0;
    connectors.forEach((item) => connectorRoot.append(card(
      item.display_name || item.id || "Connector",
      item.enabled ? "Enabled" : "Disabled",
      [`Adapter: ${item.adapter_id || "—"}`, `Credential: ${item.credential_configured ? "Configured" : "Not configured"}`],
    )));
  };

  const load = async () => {
    byId("reload").disabled = true; byId("error").textContent = "";
    try {
      const gateway = await invoke(
        gatewayContract,
        "status",
        { profile_id: profileId },
      );
      render(gateway);
    } catch (reason) {
      byId("error").textContent = reason instanceof Error ? reason.message : String(reason);
    } finally { byId("reload").disabled = false; }
  };

  byId("profile").textContent = `Profile: ${profileId}`;
  byId("reload").onclick = () => void load();
  void load();
})();

