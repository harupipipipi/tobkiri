import { api, defaultspackCanonicalRouteKey } from "../../../lib/api";
import type { CodexAppServerConfig, ModelSearchResponse } from "../../../lib/api";
import { createProviderApiKeyResources, type ProviderKeySaveResult } from "../../apiKeys";
import { createModelSearchResources } from "../../models";

const modelSearchResources = createModelSearchResources(api);
const providerApiKeyResources = createProviderApiKeyResources<Parameters<typeof api.saveProviderApiKey>[2]>(api);

export const settingsApiResources = {
  canonicalRouteKey(apiPath: string) {
    return defaultspackCanonicalRouteKey(apiPath);
  },

  searchModels(payload: { query: string; max_results: number; provider_id?: string }) {
    return modelSearchResources.searchModels(payload) as Promise<ModelSearchResponse>;
  },

  saveProviderApiKey(providerId: string, value: string, options?: Parameters<typeof api.saveProviderApiKey>[2]) {
    return providerApiKeyResources.saveProviderApiKey(providerId, value, options) as Promise<ProviderKeySaveResult>;
  },

  renameProviderApiKey(providerId: string, apiId: string, name: string) {
    return api.renameProviderApiKey(providerId, apiId, name);
  },

  deleteProviderApiKey(providerId: string, apiId: string) {
    return api.deleteProviderApiKey(providerId, apiId);
  },

  saveExternalToken(providerId: string, value: string, options?: Parameters<typeof api.saveExternalToken>[2]) {
    return api.saveExternalToken(providerId, value, options);
  },

  renameExternalToken(providerId: string, tokenId: string, name: string) {
    return api.renameExternalToken(providerId, tokenId, name);
  },

  deleteExternalToken(providerId: string, tokenId: string) {
    return api.deleteExternalToken(providerId, tokenId);
  },

  startProviderOAuth(providerId: string, options?: { scopeMode?: string; services?: string[] }) {
    return api.startProviderOAuth(providerId, options);
  },

  disconnectProviderOAuth(providerId: string) {
    return api.disconnectProviderOAuth(providerId);
  },

  clearProviderOAuthClientConfig(providerId: string) {
    return api.clearProviderOAuthClientConfig(providerId);
  },

  saveProviderOAuthClientConfig(providerId: string, clientConfig: string) {
    return api.saveProviderOAuthClientConfig(providerId, clientConfig);
  },

  importProviderConnection(providerId: string, credentialBundle: string) {
    return api.importProviderConnection(providerId, credentialBundle);
  },

  importConnectionBundle(credentialBundle: string | Record<string, unknown>, providerId?: string) {
    return api.importConnectionBundle(credentialBundle, providerId);
  },

  runProviderOAuthDiagnostics(providerId: string) {
    return api.runProviderOAuthDiagnostics(providerId);
  },

  getCodexConnectionStatus() {
    return api.getCodexConnectionStatus();
  },

  saveCodexAccessToken(accessToken: string) {
    return api.saveCodexAccessToken(accessToken);
  },

  clearCodexAccessToken() {
    return api.clearCodexAccessToken();
  },

  saveCodexAppServerConfig(config: CodexAppServerConfig) {
    return api.saveCodexAppServerConfig(config);
  },

  clearCodexAppServerConfig() {
    return api.clearCodexAppServerConfig();
  },

  probeCodexAppServer() {
    return api.probeCodexAppServer();
  },

  createPublicUrl(payload: Parameters<typeof api.createPublicUrl>[0]) {
    return api.createPublicUrl(payload);
  },

  closePublicUrl(urlId: string) {
    return api.closePublicUrl(urlId);
  },

};
