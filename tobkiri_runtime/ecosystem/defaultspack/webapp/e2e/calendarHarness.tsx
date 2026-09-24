import React from "react";
import ReactDOM from "react-dom/client";

import { CalendarComposerPanel } from "../src/App";
import "../src/index.css";

if (new URL(window.location.href).searchParams.get("manual") === "1") {
  const nativeFetch = window.fetch.bind(window);
  window.fetch = async (input, init) => {
    const url = new URL(input instanceof Request ? input.url : String(input), window.location.origin);
    const marker = "/api/contracts/defaultspack/";
    if (url.pathname.startsWith(marker)) {
      const operation = decodeURIComponent(url.pathname.slice(marker.length));
      const target = operation.slice(operation.indexOf(" ") + 1).split("?", 1)[0];
      if (target === "/api/agent/schedules") {
        return new Response(JSON.stringify({
          status: "ok",
          success: true,
          data: { schedules: [] },
          error: null,
        }), { status: 200, headers: { "Content-Type": "application/json" } });
      }
    }
    return nativeFetch(input, init);
  };
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <CalendarComposerPanel
      conversationId={null}
      modelId="stub/default"
      modelProfiles={[]}
      settings={{
        agentCurrentChat: false,
        agentModel: "",
        agentTaskDefault: false,
        defaultTime: "09:00",
        defaultItemType: "task",
        dimWeekends: true,
        eventColor: "green",
        maxItemsPerDay: 3,
        quickAddEnabled: true,
        showOutsideDays: true,
        showTimePicker: true,
        taskColor: "blue",
        timeSlotMinutes: 15,
        weekStart: "sunday",
      }}
    />
  </React.StrictMode>,
);
