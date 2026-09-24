import { useEffect, useMemo, useState } from "react";

import { CredentialTransferModal } from "../../../components/CredentialTransferModal";
import { ErrorNotice } from "../../../components/ErrorNotice";
import { cn } from "../../../lib/cn";
import { allowCleartextMobileQr } from "../../../lib/mobileCleartextQr";
import {
  apiKeySaveResource,
  buildApiKeySavePayload,
  collectApiProviderOptions,
  filterApiProviderOptionsByScope,
  filterRegisteredApiRowsByScope,
  normalizeApiProviderScope,
  requiresExplicitApiProviderProtocol,
  type ApiProviderProtocol,
} from "../../../features/apiKeys/apiKeySetup";
import {
  providerSetupLabel,
  supportsSimpleProviderSetup,
} from "../../../lib/providerPresets";
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
    () => filterApiProviderOptionsByScope(allProviderOptions, providerScope)
      .filter((option) => option.kind !== "llm" || !option.builtin || supportsSimpleProviderSetup(option.provider_id))
      .map((option) => ({
        ...option,
        label: providerSetupLabel(option.provider_id, option.label),
      })),
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
  const [protocol, setProtocol] = useState<ApiProviderProtocol>("openai-compatible");
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
  const saveResource = apiKeySaveResource(selectedKind);
  const savesExternalToken = saveResource === "external_token";
  const customLlmProtocolRequired = Boolean(providerId) && requiresExplicitApiProviderProtocol(
    providerId,
    selectedKind,
  );
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
      protocol: customLlmProtocolRequired ? protocol : undefined,
      base_url: customLlmProtocolRequired ? baseUrl : undefined,
      credential_mode: "api_key",
    });
    if (!payload) return;
    setSaveState("saving");
    setSaveError("");
    setAvailability(null);
    try {
      if (savesExternalToken) {
        await settingsApiResources.saveExternalToken(payload.provider_id, payload.value, {
          tokenId: payload.options.apiId,
          name: payload.options.name,
          kind: "token",
        });
      } else {
        const result = await settingsApiResources.saveProviderApiKey(
          payload.provider_id,
          payload.value,
          payload.options,
        );
        setAvailability(result.model_availability ?? {
          status: "route_required",
          provider_id: payload.provider_id,
          api_id: payload.options.apiId,
          candidate_models: [],
          reason: "Saved, but the backend did not confirm model availability. Choose a model route before using this key.",
        });
      }
      if (!savesExternalToken && credentialTransferEnabled) {
        setCredentialTransfer({
          providerId: payload.provider_id,
          providerLabel: selectedProviderOption?.label,
          apiId: payload.options.apiId,
        });
      }
      setSecret("");
      setBaseUrl("");
      setSaveState("saved");
      window.dispatchEvent(new Event("tobkiri-provider-connections-changed"));
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
            {savesExternalToken
              ? "外部サービスを選び、識別用の名前とトークンを入力します。"
              : customLlmProtocolRequired
                ? "Customの接続先とプロトコルを指定して、識別用の名前とAPIキーを入力します。"
                : "使いたいAIプロバイダーを選び、識別用の名前とAPIキーを入力します。接続先は自動で設定されます。"}
          </p>
          <div className="grid gap-2 md:grid-cols-[180px_minmax(120px,1fr)_minmax(180px,2fr)_auto]">
              <SearchableProviderField
                value={providerId}
                options={providerOptions}
                onChange={(nextProviderId) => {
                  setProviderId(nextProviderId);
                  setProtocol("openai-compatible");
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
                  setProtocol("openai-compatible");
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
                <input
                  type="password"
                  autoComplete="off"
                  value={secret}
                  onChange={(event) => {
                    setSecret(event.target.value);
                    resetFeedback();
                  }}
                  onKeyDown={(event) => {
                    if (event.key !== "Enter") return;
                    event.preventDefault();
                    void handleSubmit();
                  }}
                  placeholder={savesExternalToken
                    ? `${providerId || "provider"} token`
                    : `${providerId || "provider"} API key`}
                  className="min-w-0 flex-1 bg-transparent px-3 py-2 text-sm text-zinc-200 outline-none"
                />
              </div>
              <button
                type="button"
                disabled={saveState === "saving" || !providerId.trim() || !apiName.trim() || !secret.trim() || (customLlmProtocolRequired && !baseUrl.trim())}
                onClick={() => void handleSubmit()}
                className={cn(
                  "rounded-lg border px-3 py-2 text-xs transition-colors",
                  saveState !== "saving" && providerId.trim() && apiName.trim() && secret.trim() && (!customLlmProtocolRequired || baseUrl.trim())
                    ? "border-zinc-100 bg-zinc-100 text-zinc-950"
                    : "cursor-not-allowed border-zinc-800 bg-zinc-900 text-zinc-600",
                )}
              >
                {saveState === "saving"
                  ? savesExternalToken ? "保存結果を確認中" : "承認・保存結果を確認中"
                  : "Save"}
              </button>
          </div>
          {savesExternalToken ? (
            <p className="text-[11px] leading-5 text-zinc-500">
              外部サービス用トークンとして保存します。AIモデルの接続先やモデルルートには使われません。
            </p>
          ) : (
            <>
              {customLlmProtocolRequired && (
                <div className="grid gap-2 rounded-lg border border-zinc-800 bg-zinc-950/40 p-3 text-xs md:grid-cols-2">
                  <label className="space-y-1 text-[11px] text-zinc-500">
                    <span>接続プロトコル</span>
                    <select
                      aria-label="Custom LLM protocol"
                      value={protocol}
                      onChange={(event) => {
                        setProtocol(event.target.value === "anthropic" ? "anthropic" : "openai-compatible");
                        resetFeedback();
                      }}
                      className="w-full rounded-lg border border-zinc-800 bg-zinc-900 px-3 py-2 text-sm text-zinc-200 outline-none"
                    >
                      <option value="openai-compatible">OpenAI-compatible</option>
                      <option value="anthropic">Anthropic Messages</option>
                    </select>
                  </label>
                  <label className="space-y-1 text-[11px] text-zinc-500">
                    <span>HTTPS 接続先 URL</span>
                    <input
                      value={baseUrl}
                      onChange={(event) => { setBaseUrl(event.target.value); resetFeedback(); }}
                      placeholder="https://api.example.com/v1"
                      aria-label="Provider HTTPS base URL"
                      className="w-full rounded-lg border border-zinc-800 bg-zinc-900 px-3 py-2 text-sm text-zinc-200 outline-none"
                    />
                  </label>
                </div>
              )}
            </>
          )}
        </div>
        {saveState === "saved" && savesExternalToken && (
          <div className="rounded-lg border border-emerald-500/30 bg-emerald-500/10 px-3 py-2 text-[11px] text-emerald-300">
            外部サービス用トークンを保存しました。
          </div>
        )}
        {!savesExternalToken && feedback?.text && (
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
