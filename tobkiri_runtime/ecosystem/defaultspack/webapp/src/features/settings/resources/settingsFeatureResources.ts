import { api } from "../../../lib/api";
import type { McpServerRecord, PromptStudioData } from "../../../lib/api";

export type { McpServerRecord, PromptStudioData };

/** API boundary for settings surfaces that own MCP connections and prompt profiles. */
export const settingsFeatureResources = {
  listMcpServers() {
    return api.listMcpServers();
  },
  registerMcpServer(server: Parameters<typeof api.registerMcpServer>[0]) {
    return api.registerMcpServer(server);
  },
  connectMcpServer(payload: Parameters<typeof api.connectMcpServer>[0]) {
    return api.connectMcpServer(payload);
  },
  manageMcpServer(payload: Parameters<typeof api.manageMcpServer>[0]) {
    return api.manageMcpServer(payload);
  },
  getPromptStudio(params?: Parameters<typeof api.getPromptStudio>[0]) {
    return api.getPromptStudio(params);
  },
  savePrompt(payload: Parameters<typeof api.savePrompt>[0]) {
    return api.savePrompt(payload);
  },
  createPromptOverride(payload: Parameters<typeof api.createPromptOverride>[0]) {
    return api.createPromptOverride(payload);
  },
};
