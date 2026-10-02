import { api } from "../../../lib/api";
import { normalizeBrowserResult, type BrowserResult } from "../browserModel";

export const browserResources = {
  async run(action: string, payload: BrowserResult = {}) {
    return normalizeBrowserResult(await api.managedBrowser(action, payload));
  },
};
