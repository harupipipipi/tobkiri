import { useEffect, useMemo, useState } from "react";

import { CredentialTransferModal } from "../../../components/CredentialTransferModal";
import { ErrorNotice } from "../../../components/ErrorNotice";
import { cn } from "../../../lib/cn";
import { allowCleartextMobileQr } from "../../../lib/mobileCleartextQr";
import {
  buildApiKeySavePayload,
  collectApiProviderOptions,
  filterApiProviderOptionsByScope,
  filterRegisteredApiRowsByScope,
  normalizeApiProviderScope,
} from "../../../features/apiKeys/apiKeySetup";
import { settingsApiResources } from "../../../features/settings/resources/settingsApiResources";
import { availabilityCopy, type ModelAvailabilityAfterKeySave } from "../../../features/settings/resources/useModelAvailability";
import type { SettingsFieldRendererProps } from "../fieldRendererRegistry";
import { SearchableProviderField } from "./providerSelectField";
import {
  apiKeySetupTargetFieldId,
  fieldOptionProviderRows,
  fieldProviderRows,
  registeredApiRows,
  selectedProviderKind,
  SettingsFieldShell,
} from "./settingsFieldRendererUtils";

export function BuiltinApiKeySetupRenderer({ sectionId, field, value, sectionValues, onChange }: SettingsFieldRendererProps) {
  const targetFieldId = apiKeySetupTargetFieldId(field);
  const providers = fieldProviderRows(field, sectionValues);
  const providerScope = normalizeApiProviderScope((field as unknown as Record<string, unknown>).provider_scope);
  const allProviderOptions = useMemo(() => collectApiProviderOptions([
    ...fieldOptionProviderRows(field),
    ...providers,
  ]), [field, providers]);
  const providerOptions = useMemo(
    () => filterApiProviderOptionsByScope(allProviderOptions, providerScope),
    [allProviderOptions, providerScope],
  );
  const registeredApis = filterRegisteredApiRowsByScope(
    registeredApiRows(providers),
    allProviderOptions,
    providerScope,
  );
  const [providerId, setProviderId] = useState(String(field.provider_id ?? ""));
  const [apiName, setApiName] = useState("main");
  const [secret, setSecret] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [allowedModels, setAllowedModels] = useState("");
  const [defaultModel, setDefaultModel] = useState("");
  const [quotaLabel, setQuotaLabel] = useState("");
  const [notes, setNotes] = useState("");
  const [credentialMode, setCredentialMode] = useState<"api_key" | "none">("api_key");
  const [saveState, setSaveState] = useState<"idle" | "saving" | "saved">("idle");
  const [saveError, setSaveError] = useState("");
  const [availability, setAvailability] = useState<ModelAvailabilityAfterKeySave | null>(null);
  const [credentialTransfer, setCredentialTransfer] = useState<{
    providerId: string;
    providerLabel?: string;
    apiId: string;
  } | null>(null);
  const selectedProviderOption = providerOptions.find((option) => option.provider_id === providerId);
  const selectedKind = selectedProviderKind(providerId, providerOptions);
  const credentialTransferEnabled = allowCleartextMobileQr();
  const feedback = saveState === "saved" ? availabilityCopy(availability) : null;

  useEffect(() => {
    if (!providerId) return;
    if (providerOptions.some((option) => option.provider_id === providerId)) return;
    setProviderId("");
  }, [providerId, providerOptions]);

  const resetFeedback = () => {
    setSaveState("idle");
    setSaveError("");
    setAvailability(null);
  };

  const handleSubmit = async () => {
    const payload = buildApiKeySavePayload({
      provider_id: providerId,
      name: apiName,
      value: secret,
      kind: selectedKind,
      base_url: baseUrl,
      allowed_models: allowedModels,
      default_model: defaultModel,
      quota_label: quotaLabel,
      notes,
      credential_mode: credentialMode,
    });
    if (!payload) return;
    setSaveState("saving");
    setSaveError("");
    setAvailability(null);
    try {
      const result = await settingsApiResources.saveProviderApiKey(payload.provider_id, payload.value, payload.options);
      setAvailability(result.model_availability ?? {
        status: "route_required",
        provider_id: payload.provider_id,
        api_id: payload.options.apiId,
        candidate_models: [],
        reason: "Saved, but the backend did not confirm model availability. Choose a model route before using this key.",
      });
      if (credentialMode === "api_key" && credentialTransferEnabled) {
        setCredentialTransfer({
          providerId: payload.provider_id,
          providerLabel: selectedProviderOption?.label,
          apiId: payload.options.apiId,
        });
      }
      setSecret("");
      setBaseUrl("");
      setAllowedModels("");
      setDefaultModel("");
      setQuotaLabel("");
      setNotes("");
      setSaveState("saved");
    } catch (saveErrorValue) {
      setSaveState("idle");
      setSaveError(saveErrorValue instanceof Error ? saveErrorValue.message : "API key save failed.");
    }
  };

  return (
    <SettingsFieldShell field={field}>
      <div
        className="space-y-3"
        data-settings-renderer="api_key_setup"
        data-provider-scope={providerScope}
      >
        {registeredApis.length > 0 && (
          <div className="overflow-hidden rounded-lg border border-zinc-800 bg-zinc-950/70">
            {registeredApis.map((api) => (
              <div key={String(api.key ?? `${api.provider_id}:${api.api_id}`)} className="flex flex-wrap items-center gap-2 border-b border-zinc-800/80 px-3 py-2.5 last:border-b-0">
                <span className="text-sm font-medium text-zinc-200">{String(api.name ?? api.api_id ?? "")}</span>
                <span className="rounded-full border border-zinc-700 bg-zinc-900 px-2 py-0.5 text-[10px] uppercase tracking-wide text-zinc-400">
                  {String(api.provider_id ?? "")}
                </span>
                <span className="font-mono text-xs text-zinc-500">{String(api.provider_id ?? "")}:{String(api.api_id ?? "")}:***</span>
              </div>
            ))}
          </div>
        )}
        <div className="space-y-3 rounded-xl border border-white/[0.08] bg-white/[0.025] p-3">
          <p className="text-xs leading-5 text-zinc-500">
            使いたいAIプロバイダーを選び、識別用の名前とAPIキーを入力します。
          </p>
          <div className="grid gap-2 md:grid-cols-[180px_minmax(120px,1fr)_minmax(180px,2fr)_auto]">
              <SearchableProviderField
                value={providerId}
                options={providerOptions}
                onChange={(nextProviderId) => {
                  setProviderId(nextProviderId);
                  resetFeedback();
                }}
                onAddCustom={(option) => {
                  onChange(sectionId, targetFieldId, {
                    action: "register_provider",
                    provider_id: option.providerId,
                    label: option.label,
                    kind: option.kind,
                  });
                  setProviderId(option.providerId);
                  resetFeedback();
                }}
              />
              <input
                value={apiName}
                onChange={(event) => {
                  setApiName(event.target.value);
                  resetFeedback();
                }}
                placeholder="名前 (例: main, work)"
                className="rounded-lg border border-zinc-800 bg-zinc-900 px-3 py-2 text-sm text-zinc-200 outline-none"
              />
              <div className="flex rounded-lg border border-zinc-800 bg-zinc-900 focus-within:border-zinc-600">
                <select value={credentialMode} onChange={(event) => { setCredentialMode(event.target.value === "none" ? "none" : "api_key"); resetFeedback(); }} className="max-w-20 bg-transparent px-2 text-[10px] text-zinc-400 outline-none">
                  <option value="api_key">Key</option>
                  <option value="none">Local</option>
                </select>
                <input
                  type="password"
                  autoComplete="off"
                  value={secret}
                  disabled={credentialMode === "none"}
                  onChange={(event) => {
                    setSecret(event.target.value);
                    resetFeedback();
                  }}
                  onKeyDown={(event) => {
                    if (event.key !== "Enter") return;
                    event.preventDefault();
                    void handleSubmit();
                  }}
                  placeholder={credentialMode === "none" ? "loopback endpoint only" : `${providerId || "provider"} API key`}
                  className="min-w-0 flex-1 bg-transparent px-2 py-2 text-sm text-zinc-200 outline-none disabled:cursor-not-allowed disabled:text-zinc-600"
                />
              </div>
              <button
                type="button"
                disabled={saveState === "saving" || !providerId.trim() || !apiName.trim() || (credentialMode === "api_key" ? !secret.trim() : !baseUrl.trim())}
                onClick={() => void handleSubmit()}
                className={cn(
                  "rounded-lg border px-3 py-2 text-xs transition-colors",
                  saveState !== "saving" && providerId.trim() && apiName.trim() && (credentialMode === "api_key" ? secret.trim() : baseUrl.trim())
                    ? "border-zinc-100 bg-zinc-100 text-zinc-950"
                    : "cursor-not-allowed border-zinc-800 bg-zinc-900 text-zinc-600",
                )}
              >
                {saveState === "saving" ? "Saving" : "Save"}
              </button>
          </div>
          <details className="rounded-lg border border-zinc-800 bg-zinc-950/40 px-3 py-2 text-xs">
            <summary className="cursor-pointer select-none text-zinc-400 hover:text-zinc-200">詳細設定（任意）</summary>
            <div className="mt-3 grid gap-2 md:grid-cols-2">
              <input value={baseUrl} onChange={(event) => { setBaseUrl(event.target.value); resetFeedback(); }} placeholder="base_url (optional)" className="rounded-lg border border-zinc-800 bg-zinc-900 px-3 py-2 text-sm text-zinc-200 outline-none" />
              <input value={defaultModel} onChange={(event) => { setDefaultModel(event.target.value); resetFeedback(); }} placeholder="default model for this API" className="rounded-lg border border-zinc-800 bg-zinc-900 px-3 py-2 text-sm text-zinc-200 outline-none" />
              <input value={allowedModels} onChange={(event) => { setAllowedModels(event.target.value); resetFeedback(); }} placeholder="allowed models, comma separated" className="rounded-lg border border-zinc-800 bg-zinc-900 px-3 py-2 text-sm text-zinc-200 outline-none" />
              <input value={quotaLabel} onChange={(event) => { setQuotaLabel(event.target.value); resetFeedback(); }} placeholder="quota label" className="rounded-lg border border-zinc-800 bg-zinc-900 px-3 py-2 text-sm text-zinc-200 outline-none" />
              <textarea value={notes} onChange={(event) => { setNotes(event.target.value); resetFeedback(); }} placeholder="notes for routing" className="min-h-20 rounded-lg border border-zinc-800 bg-zinc-900 px-3 py-2 text-sm text-zinc-200 outline-none md:col-span-2" />
            </div>
          </details>
        </div>
        {feedback?.text && (
          feedback.tone === "success" ? (
            <div className="rounded-lg border border-emerald-500/30 bg-emerald-500/10 px-3 py-2 text-[11px] text-emerald-300">
              {feedback.text}
            </div>
          ) : (
            <ErrorNotice
              className="px-3 py-2 text-[11px]"
              copyLabel="APIキー設定の警告をコピー"
              message={feedback.text}
              severity="warning"
            />
          )
        )}
        {saveError && (
          <ErrorNotice
            className="px-3 py-2 text-[11px]"
            copyLabel="APIキー保存エラーをコピー"
            message={saveError}
          />
        )}
        {credentialTransfer && (
          <CredentialTransferModal
            providerId={credentialTransfer.providerId}
            providerLabel={credentialTransfer.providerLabel}
            apiId={credentialTransfer.apiId}
            onClose={() => {
              setCredentialTransfer(null);
              onChange(sectionId, targetFieldId, { action: "oauth_refresh" });
            }}
          />
        )}
      </div>
    </SettingsFieldShell>
  );
}
