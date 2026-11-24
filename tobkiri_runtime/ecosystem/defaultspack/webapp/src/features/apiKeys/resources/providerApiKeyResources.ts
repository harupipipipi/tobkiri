import type { ModelAvailabilityAfterKeySave } from "../../../lib/api";

export type ProviderKeySaveResult = {
  provider_id: string;
  api_id?: string;
  name?: string;
  provider_instance_id?: string;
  configured: boolean;
  kind?: string;
  model_availability?: ModelAvailabilityAfterKeySave;
};

export type ProviderApiKeyClient<SaveOptions> = {
  saveProviderApiKey(providerId: string, value: string, options?: SaveOptions): Promise<ProviderKeySaveResult>;
};

export function createProviderApiKeyResources<SaveOptions>(apiClient: ProviderApiKeyClient<SaveOptions>) {
  return {
    saveProviderApiKey(providerId: string, value: string, options?: SaveOptions) {
      return apiClient.saveProviderApiKey(providerId, value, options);
    },
  };
}
