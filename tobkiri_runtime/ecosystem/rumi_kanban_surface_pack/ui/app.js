(() => {
  "use strict";

  const contract = "rumi.resource.kanban.v1";
  const nonce = new URLSearchParams(location.hash.slice(1)).get("rumi_rpc_nonce");
  const profileId = new URLSearchParams(location.search).get("profile_id") || "";
  const pending = new Map();
  const byId = (id) => document.getElementById(id);

  addEventListener("message", (event) => {
    if (event.source !== parent || event.origin !== location.origin) return;
    const response = event.data;
    if (
      !response
      || response.type !== "rumi.capability.response"
      || response.nonce !== nonce
    ) return;
    const request = pending.get(response.requestId);
    if (!request) return;
    pending.delete(response.requestId);
    clearTimeout(request.timer);
    response.ok
      ? request.resolve(response.value)
      : request.reject(new Error(response.error || "capability_unavailable"));
  });

  const invoke = (operation, input) => new Promise((resolve, reject) => {
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
      contractId: contract,
      payload: { operation, input },
    }, location.origin);
  });

  const boardCard = (board) => {
    const article = document.createElement("article");
    const title = document.createElement("h3");
    title.textContent = board.title || board.id || "Kanban board";
    const scope = document.createElement("span");
    scope.className = "scope";
    scope.textContent = `${board.scope?.type || "profile"}: ${board.scope?.id || "default"}`;
    const counts = document.createElement("span");
    counts.className = "meta";
    counts.textContent = `${board.column_count || 0} columns · ${board.card_count || 0} cards`;
    article.append(title, scope, counts);
    return article;
  };

  const render = (snapshot) => {
    const boards = Array.isArray(snapshot?.boards) ? snapshot.boards : [];
    byId("board-count").textContent = String(boards.length);
    const cards = boards.reduce(
      (sum, board) => sum + Number(board.card_count || 0),
      0,
    );
    byId("card-count").textContent = String(cards);
    byId("revision").textContent = String(snapshot?.revision ?? "—");
    const root = byId("boards");
    root.replaceChildren();
    byId("empty").hidden = boards.length !== 0;
    boards.forEach((board) => root.append(boardCard(board)));
  };

  const load = async () => {
    byId("reload").disabled = true;
    byId("error").textContent = "";
    try {
      render(await invoke("list", { profile_id: profileId }));
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

