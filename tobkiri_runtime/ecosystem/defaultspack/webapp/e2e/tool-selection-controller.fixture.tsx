import { act, useState } from "react";
import { createRoot } from "react-dom/client";

import { api } from "../src/lib/api";
import type { ConversationToolPreferences } from "../src/features/tools/types";
import { useToolSelectionController } from "../src/features/tools/useToolSelectionController";

// This fixture mounts the actual hook in a browser. It does not add a runtime
// route or relax the Host contract for the currently unavailable review API.
export async function exerciseToolSelectionControllerReset() {
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  const settingsValues = { tools: { default_mode: "auto" } };
  let conversationPreferences: ConversationToolPreferences = {
    mode: "manual",
    include: [{ kind: "service", id: "github" }],
    exclude: [{ kind: "tool", id: "computer_control" }],
  };
  let controller: ReturnType<typeof useToolSelectionController> | null = null;
  let selectedToolIds: string[] = [];
  const originalPreview = api.previewToolSelection;
  const actEnvironment = globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT?: boolean };
  const originalActEnvironment = actEnvironment.IS_REACT_ACT_ENVIRONMENT;
  actEnvironment.IS_REACT_ACT_ENVIRONMENT = true;

  function Harness() {
    const [toolIds, setToolIds] = useState(["web_search"]);
    selectedToolIds = toolIds;
    controller = useToolSelectionController({
      settingsValues,
      selectedToolIds: toolIds,
      setSelectedToolIds: setToolIds,
      conversationPreferences,
    });
    return null;
  }

  function current() {
    if (!controller) throw new Error("The tool selection controller is not mounted");
    return controller;
  }

  function snapshot() {
    return {
      ...current().state,
      selectedToolIds: [...selectedToolIds],
      request: current().buildRequest({ toolIds: selectedToolIds }),
    };
  }

  try {
    await act(async () => { root.render(<Harness />); });
    await act(async () => {
      current().setTurnMode("review");
      current().removeTarget({
        ...current().state.overrideChips.find((chip) => chip.kind === "service" && chip.id === "github")!,
      });
    });
    api.previewToolSelection = async () => ({
      preview_id: "controller-reset-preview",
      expires_at: "2099-01-01T00:00:00Z",
      decision: {
        selected_tools: ["web_search"],
        selected_services: [],
        recommendations: [],
        permission_summary: { auto: 1, confirm: 0 },
      },
    });
    await act(async () => {
      await current().previewReview({
        conversationId: "existing-conversation",
        userText: "Draft awaiting tool review",
        toolSelection: current().buildRequest({ toolIds: selectedToolIds }),
        draft: { input: "Draft awaiting tool review", attachments: [], droppedWidgets: [] },
      });
    });
    const beforeReset = snapshot();

    await act(async () => { current().resetDraft(); });
    const afterReset = snapshot();

    conversationPreferences = {};
    await act(async () => { root.render(<Harness />); });
    const afterNewConversation = snapshot();

    return { beforeReset, afterReset, afterNewConversation, settingsValues };
  } finally {
    api.previewToolSelection = originalPreview;
    await act(async () => { root.unmount(); });
    container.remove();
    actEnvironment.IS_REACT_ACT_ENVIRONMENT = originalActEnvironment;
  }
}
