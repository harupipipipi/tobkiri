import { api } from "../../../lib/api";
import { createProviderModelAccessResources } from "./providerModelAccessResources";

/** Stable resource uses only authenticated finite Host ports. */
export const nativeProviderModelAccessResources = createProviderModelAccessResources({
  getModelAccess: (scope) => api.getModelAccess(scope),
  setModelAccess: (input) => api.setModelAccess(input),
  getModelAccessCatalog: (input) => api.getModelAccessCatalog(input),
});
