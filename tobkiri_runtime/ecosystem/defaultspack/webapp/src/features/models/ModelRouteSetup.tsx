import { useEffect, useId, useMemo, useRef, useState } from "react";

import { ErrorNotice } from "../../components/ErrorNotice";
import type { RegisteredProviderConnection } from "../../lib/api";
import { redactDiagnosticText } from "../../lib/clientDiagnostics";
import {
  catalogModelProfileId,
  connectionCatalogProviderId,
  providerCatalogModels,
  type ProviderCatalogModel,
} from "../../lib/providerCatalog";
import type { ModelPropertiesRequest } from "../search/modelPropertiesNavigation";
import { ModelSearchPicker } from "./ModelSearchPicker";
import { modelSearchItemToModelSelectOption, type ModelSelectOption } from "./modelSelect";
import { modelRouteConnectionLabel } from "./modelRoutePresentation";
import { settingsApiResources } from "../settings/resources/settingsApiResources";

/** Return a user-visible model route error without exposing connection secrets. */
export function modelRouteSaveErrorMessage(error: unknown): string {
  const detail = redactDiagnosticText(
    error instanceof Error ? error.message : error,
    320,
  );
  const prefix = "モデルルートを保存できませんでした。接続とモデル設定を確認してから保存し直してください。";
  return detail ? `${prefix} 詳細: ${detail}` : prefix;
}

/** Return a user-visible connection-list error without exposing secrets. */
export function modelRouteConnectionsErrorMessage(error: unknown): string {
  const detail = redactDiagnosticText(
    error instanceof Error ? error.message : error,
    320,
  );
  const prefix = "Provider接続一覧を確認できません。再読み込みしてから設定してください。";
  return detail ? `${prefix} 詳細: ${detail}` : prefix;
}

export function ModelRouteErrorNotices({
  connectionsError,
  saveError,
}: {
  connectionsError: string;
  saveError: string;
}) {
  return <>
    {connectionsError && <ErrorNotice
      copyLabel="Provider接続一覧エラーをコピー"
      errorIcon="model-route-provider-connections"
      message={connectionsError}
      severity="warning"
      title="Provider接続一覧を確認できません"
    />}
    {saveError && <ErrorNotice
      copyLabel="モデルルート保存エラーをコピー"
      errorIcon="model-route-save"
      message={saveError}
      title="モデルルートを保存できません"
    />}
  </>;
}

export function ProviderReadiness({
  connection,
}: {
  connection: RegisteredProviderConnection;
}) {
  const credential = connection.credential_status === "configured"
    ? "資格情報: 設定済み"
    : connection.credential_status === "not_required"
      ? "資格情報: 不要（ローカル接続）"
      : "資格情報: 未設定";
  const reachability = connection.reachability === "available"
    ? "到達性: 利用可能"
    : connection.reachability === "unavailable"
      ? "到達性: 利用不可"
      : "到達性: 未確認";
  const health = connection.health_status === "verified"
    ? "検証済み"
    : "未検証";
  return <p className="text-xs text-zinc-400">
    {credential} / {reachability}（{health}）
  </p>;
}

/** Catalog choices never attest account access or runtime capabilities. */
export function CatalogModelPicker({ models, value, query, providerId = "", connectionId, selectedOption, onChange, onQueryChange, onSelectedOptionChange }: {
  models: readonly ProviderCatalogModel[];
  value: string;
  query: string;
  providerId?: string;
  connectionId?: string;
  selectedOption?: ModelSelectOption | null;
  onChange: (value: string) => void;
  onQueryChange: (query: string) => void;
  onSelectedOptionChange?: (option: ModelSelectOption | null) => void;
}) {
  const options: ModelSelectOption[] = models.map((model) => ({
    value: model.model_id, label: model.display_name, model_id: model.model_id,
    provider_id: providerId || undefined,
  }));
  if (selectedOption?.value === value && !options.some((option) => option.value === value)) options.push(selectedOption);
  return <div className="grid gap-2">
    <ModelSearchPicker value={value} options={options} query={query} onChange={onChange}
      onQueryChange={onQueryChange} onSelectedOptionChange={onSelectedOptionChange}
      showTrigger={false} open preset={{ kinds: ["model"], ...(providerId ? { providerIds: [providerId] } : {}), connectionId }}
      catalogOptionAdapter={(item) => {
        if (!providerId || item.provider_id !== providerId || !item.model_id
          || (connectionId && item.connection_id !== connectionId)) return null;
        return { ...modelSearchItemToModelSelectOption(item), value: item.model_id };
      }} />
    {value && <p className="text-xs text-zinc-400">選択中: <span className="font-mono text-zinc-200">{value}</span></p>}
    <p className="text-xs text-zinc-500">Providerのモデルカタログです。このAPIでの利用可否は未確認です。</p>
  </div>;
}

/** Advanced editing hides controls while keeping the current custom draft visible. */
export function CustomModelControls({ displayMode, manual, manualModel, hasCatalog, model, onManualChange, onChange }: {
  displayMode: "standard" | "advanced";
  manual: boolean;
  manualModel: boolean;
  hasCatalog: boolean;
  model: string;
  onManualChange: (manual: boolean) => void;
  onChange: (model: string) => void;
}) {
  if (displayMode === "standard") return manualModel ? <p role="status" className="text-xs text-zinc-400">
    カスタムモデル: <span className="font-mono text-zinc-200">{model || "未入力"}</span>
    <span className="block">モデルIDを編集するには、表示をAdvancedに切り替えてください。</span>
  </p> : null;
  return <details open={manualModel}>
    <summary className="cursor-pointer text-xs text-zinc-400">カスタムモデル・一覧にないモデル</summary>
    {hasCatalog && <label className="mt-2 flex items-center gap-2 text-xs">
      <input type="checkbox" checked={manual} onChange={(event) => onManualChange(event.target.checked)} />モデルIDを指定する
    </label>}
    {manualModel && <label className="mt-2 grid gap-1 text-xs">モデルID
      <input className="rounded border border-zinc-700 bg-zinc-950 px-2 py-2 text-sm text-zinc-100" value={model}
        autoCorrect="off" autoCapitalize="none" spellCheck={false}
        onChange={(event) => onChange(event.target.value)} placeholder="ProviderのモデルID" />
    </label>}
  </details>;
}

/** Choose a catalog model for the exact saved connection without typing IDs. */
export function ModelRouteSetup({ preferredConnectionId = "", displayMode = "standard", requestedModel, onModelPropertiesAcknowledged }: {
  preferredConnectionId?: string;
  displayMode?: "standard" | "advanced";
  requestedModel?: ModelPropertiesRequest | null;
  onModelPropertiesAcknowledged?: (request: ModelPropertiesRequest) => void;
}) {
  const connectionSelectId = useId();
  const [catalogModel, setCatalogModel] = useState("");
  const [selectedCatalogOption, setSelectedCatalogOption] = useState<ModelSelectOption | null>(null);
  const [customModel, setCustomModel] = useState("");
  const [query, setQuery] = useState("");
  const [provider, setProvider] = useState("");
  const [manual, setManual] = useState(false);
  const [connections, setConnections] = useState<RegisteredProviderConnection[]>([]);
  const [providerRegistryRevision, setProviderRegistryRevision] = useState<number | null>(null);
  const [connectionsError, setConnectionsError] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [saveError, setSaveError] = useState("");

  const lastIncoming = useRef<{ requestId?: number; preferredId: string } | null>(null);
  useEffect(() => {
    let active = true;
    let version = 0;
    let incomingApplied = false;
    const newRequest = lastIncoming.current?.requestId !== requestedModel?.requestId;
    const incomingConnectionId = newRequest
      ? requestedModel?.identity.connectionId || preferredConnectionId
      : preferredConnectionId;
    lastIncoming.current = { requestId: requestedModel?.requestId, preferredId: preferredConnectionId };
    const loadConnections = async () => {
      const currentVersion = ++version;
      try {
        const snapshot = await settingsApiResources.listProviderConnections();
        if (!active || currentVersion !== version) return;
        setConnections(snapshot.connections);
        setProviderRegistryRevision(snapshot.registry_revision);
        setConnectionsError("");
        setProvider((current) => {
          if (!incomingApplied && incomingConnectionId
            && snapshot.connections.some((connection) => connection.provider_instance_id === incomingConnectionId)) {
            incomingApplied = true;
            return incomingConnectionId;
          }
          const desired = current;
          if (snapshot.connections.some((connection) => connection.provider_instance_id === desired)) return desired;
          return !requestedModel && snapshot.connections.length === 1 ? snapshot.connections[0].provider_instance_id : "";
        });
      } catch (error) {
        if (!active || currentVersion !== version) return;
        setConnections([]);
        setProviderRegistryRevision(null);
        setProvider("");
        setConnectionsError(modelRouteConnectionsErrorMessage(error));
      }
    };
    void loadConnections();
    window.addEventListener("tobkiri-provider-connections-changed", loadConnections);
    return () => {
      active = false;
      window.removeEventListener("tobkiri-provider-connections-changed", loadConnections);
    };
  }, [preferredConnectionId, requestedModel?.requestId]);


  const selectedConnection = connections.find(
    (connection) => connection.provider_instance_id === provider,
  );
  const catalogProviderId = selectedConnection ? connectionCatalogProviderId(selectedConnection) : "";
  const catalogModels = useMemo(() => providerCatalogModels(catalogProviderId)
    .filter((item) => ["chat", "reasoning"].includes(item.type)), [catalogProviderId]);
  useEffect(() => {
    setSelectedCatalogOption(null);
    setCatalogModel("");
    setCustomModel("");
    setQuery("");
    setManual(false);
    setMessage("");
    setSaveError("");
  }, [provider, catalogProviderId, requestedModel?.requestId]);

  const manualModel = manual || (Boolean(selectedConnection) && !catalogProviderId);
  const model = manualModel ? customModel : catalogModel;
  const selectedModel = catalogModels.find((item) => item.model_id === model);
  useEffect(() => {
    if (!requestedModel || requestedModel.registered || !selectedConnection) return;
    const identity = requestedModel.identity;
    if (identity.providerId !== catalogProviderId
      || (identity.connectionId && identity.connectionId !== selectedConnection.provider_instance_id)) return;
    const option = modelSearchItemToModelSelectOption(requestedModel.model);
    setManual(false);
    setCatalogModel(identity.modelId);
    setSelectedCatalogOption({ ...option, value: identity.modelId });
  }, [requestedModel?.requestId, provider, catalogProviderId]);
  useEffect(() => {
    if (!requestedModel || requestedModel.registered || !selectedConnection
      || providerRegistryRevision === null || manualModel
      || catalogProviderId !== requestedModel.identity.providerId
      || (requestedModel.identity.connectionId
        && requestedModel.identity.connectionId !== selectedConnection.provider_instance_id)
      || model !== requestedModel.identity.modelId
      || selectedCatalogOption?.value !== requestedModel.identity.modelId) return;
    onModelPropertiesAcknowledged?.(requestedModel);
  }, [requestedModel, selectedConnection, providerRegistryRevision, manualModel,
    catalogProviderId, model, selectedCatalogOption, onModelPropertiesAcknowledged]);
  const validModel = Boolean(model.trim()) && (manualModel || Boolean(selectedModel)
    || selectedCatalogOption?.value === model);
  const save = async () => {
    if (!selectedConnection || providerRegistryRevision === null || !validModel) return;
    setBusy(true);
    setMessage("");
    setSaveError("");
    try {
      const profileId = await catalogModelProfileId(selectedConnection.provider_instance_id, model.trim());
      await settingsApiResources.createModelProfile({
        model_profile_id: profileId,
        model_id: model.trim(),
        provider_instance_id: selectedConnection.provider_instance_id,
        display_name: `${selectedModel?.display_name || selectedCatalogOption?.label || model.trim()} (${selectedConnection.display_name})`,
        provider_registry_revision: providerRegistryRevision,
      });
      setMessage("モデル設定を保存しました。チャットのモデル一覧から選択できます。");
      window.dispatchEvent(new Event("tobkiri-model-profiles-changed"));
    } catch (error) {
      setSaveError(modelRouteSaveErrorMessage(error));
    } finally {
      setBusy(false);
    }
  };
  const field = "rounded border border-zinc-700 bg-zinc-950 px-2 py-2 text-sm text-zinc-100";
  return <fieldset disabled={busy} className="mt-3 grid gap-2 rounded-lg border border-zinc-700 p-3">
    <legend className="text-sm text-zinc-200">使いたいモデルを選ぶ</legend>
    {requestedModel && <div className="rounded border border-zinc-700 p-2 text-xs text-zinc-400" data-model-properties-draft>
      <p className="font-medium text-zinc-200">{requestedModel.model.display_name}</p>
      <p>Provider: {requestedModel.identity.providerId}</p>
      <p className="font-mono">{requestedModel.identity.modelId}</p>
      <p>保存するには、このProviderの使用するAPIを選んでください。</p>
    </div>}
    <label htmlFor={connectionSelectId} className="grid gap-1 text-xs">使用するAPI
      <select id={connectionSelectId} aria-label="使用するAPI" className={field} value={provider} onChange={(event) => setProvider(event.target.value)} disabled={!connections.length}>
        <option value="">{connections.length ? "接続を選択" : "登録済みの接続がありません"}</option>
        {connections.map((connection) => <option key={connection.provider_instance_id} value={connection.provider_instance_id}>{modelRouteConnectionLabel(connection, connections)}</option>)}
      </select>
    </label>
    {selectedConnection && <>
      <ProviderReadiness connection={selectedConnection} />
      {!manualModel && <CatalogModelPicker models={catalogModels} value={model} query={query}
        providerId={catalogProviderId} connectionId={selectedConnection.provider_instance_id}
        selectedOption={selectedCatalogOption} onSelectedOptionChange={setSelectedCatalogOption}
        onChange={(next) => { setCatalogModel(next); setMessage(""); }} onQueryChange={setQuery} />}
      <CustomModelControls
        displayMode={displayMode}
        manual={manual}
        manualModel={manualModel}
        hasCatalog={Boolean(catalogProviderId)}
        model={customModel}
        onManualChange={setManual}
        onChange={setCustomModel}
      />
    </>}
    <ModelRouteErrorNotices
      connectionsError={connectionsError}
      saveError={saveError}
    />
    <button type="button" disabled={busy || !validModel || !selectedConnection || providerRegistryRevision === null} onClick={() => void save()} className="rounded border border-zinc-600 px-3 py-2 text-sm disabled:opacity-50">{busy ? "保存結果を確認中" : "このモデルを使う"}</button>
    {message && <p role="status" className="text-xs text-zinc-300">{message}</p>}
  </fieldset>;
}
