(() => {
  "use strict";

  const contracts = {
    schedules: "rumi.resource.schedule.v1",
    runtime: "rumi.resource.scheduler.v1",
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

  const date = (value) => {
    const number = Number(value || 0);
    if (!number) return "—";
    const parsed = new Date(number);
    return Number.isNaN(parsed.getTime()) ? "—" : parsed.toLocaleString();
  };

  const render = (snapshot, runtime) => {
    const schedules = Array.isArray(snapshot?.schedules) ? snapshot.schedules : [];
    byId("runtime-state").textContent = runtime?.stopping ? "Stopping" : "Ready";
    byId("schedule-count").textContent = String(schedules.length);
    byId("last-tick").textContent = date(runtime?.last_tick_at_ms);
    const body = byId("schedules");
    body.replaceChildren();
    byId("empty").hidden = schedules.length !== 0;
    schedules.forEach((schedule) => {
      const row = document.createElement("tr");
      const values = [
        schedule.name || schedule.id || "—",
        schedule.action_id || "—",
        schedule.status || "—",
        date(schedule.next_run_at_ms),
        `${Number(schedule.attempt || 0)} / ${Number(schedule.max_attempts || 0)}`,
      ];
      values.forEach((value, index) => {
        const cell = document.createElement("td");
        if (index === 2) {
          const status = document.createElement("span");
          status.className = "status";
          status.textContent = String(value);
          cell.append(status);
        } else {
          cell.textContent = String(value);
        }
        row.append(cell);
      });
      body.append(row);
    });
  };

  const load = async () => {
    byId("reload").disabled = true;
    byId("error").textContent = "";
    try {
      const [snapshot, runtime] = await Promise.all([
        invoke(contracts.schedules, "list", { profile_id: profileId }),
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

