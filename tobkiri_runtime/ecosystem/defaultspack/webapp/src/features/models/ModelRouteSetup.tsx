import { useEffect, useMemo, useState } from "react";

import { ErrorNotice } from "../../components/ErrorNotice";
import type { RegisteredProviderConnection } from "../../lib/api";
import { redactDiagnosticText } from "../../lib/clientDiagnostics";
import {
  catalogModelProfileId,
  connectionCatalogProviderId,
  providerCatalogModels,
  type ProviderCatalogModel,
} from "../../lib/providerCatalog";
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
export function CatalogModelPicker({ models, value, query, onChange, onQueryChange }: {
  models: readonly ProviderCatalogModel[];
  value: string;
  query: string;
  onChange: (value: string) => void;
  onQueryChange: (query: string) => void;
}) {
  const search = query.trim().toLowerCase();
  const choices = models.filter((model) => model.model_id === value
    || `${model.model_id} ${model.display_name}`.toLowerCase().includes(search));
  const field = "rounded border border-zinc-700 bg-zinc-950 px-2 py-2 text-sm text-zinc-100";
  return <div className="grid gap-2">
    <label className="grid gap-1 text-xs">モデルを検索
      <input type="search" className={field} aria-label="モデルを検索" value={query}
        autoCorrect="off" autoCapitalize="none" spellCheck={false}
        onChange={(event) => onQueryChange(event.target.value)} placeholder="名前で検索" />
    </label>
    <label className="grid gap-1 text-xs">モデル一覧（{models.length}件）
      <select aria-label="モデル一覧" className={field} value={value} onChange={(event) => onChange(event.target.value)}>
        <option value="">使いたいモデルを選択</option>
        {choices.map((model) => <option key={model.model_id} value={model.model_id}>{model.display_name} — {model.model_id}</option>)}
      </select>
    </label>
    {search && choices.length === 0 && <p role="status" className="text-xs text-zinc-400">該当するモデルがありません。</p>}
  </div>;
}

/** Choose a catalog model for the exact saved connection without typing IDs. */
export function ModelRouteSetup({ preferredConnectionId = "" }: { preferredConnectionId?: string }) {
  const [model, setModel] = useState("");
  const [query, setQuery] = useState("");
  const [provider, setProvider] = useState("");
  const [manual, setManual] = useState(false);
  const [connections, setConnections] = useState<RegisteredProviderConnection[]>([]);
  const [providerRegistryRevision, setProviderRegistryRevision] = useState<number | null>(null);
  const [connectionsError, setConnectionsError] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [saveError, setSaveError] = useState("");

  useEffect(() => {
    let active = true;
    let version = 0;
    const loadConnections = async () => {
      const currentVersion = ++version;
      try {
        const snapshot = await settingsApiResources.listProviderConnections();
        if (!active || currentVersion !== version) return;
        setConnections(snapshot.connections);
        setProviderRegistryRevision(snapshot.registry_revision);
        setConnectionsError("");
        setProvider((current) => {
          const desired = preferredConnectionId || current;
          if (snapshot.connections.some((connection) => connection.provider_instance_id === desired)) return desired;
          return snapshot.connections.length === 1 ? snapshot.connections[0].provider_instance_id : "";
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
  }, [preferredConnectionId]);

  useEffect(() => {
    setModel("");
    setQuery("");
    setManual(false);
    setMessage("");
    setSaveError("");
  }, [provider]);

  const selectedConnection = connections.find(
    (connection) => connection.provider_instance_id === provider,
  );
  const catalogProviderId = selectedConnection ? connectionCatalogProviderId(selectedConnection) : "";
  const catalogModels = useMemo(() => providerCatalogModels(catalogProviderId)
    .filter((item) => ["chat", "reasoning"].includes(item.type)), [catalogProviderId]);
  const selectedModel = catalogModels.find((item) => item.model_id === model);
  const manualModel = manual || (Boolean(selectedConnection) && !catalogModels.length);
  const validModel = Boolean(model.trim()) && (manualModel || Boolean(selectedModel));
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
        display_name: `${selectedModel?.display_name || model.trim()} (${selectedConnection.display_name})`,
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
    <label className="grid gap-1 text-xs">API接続
      <select aria-label="Provider connection ID" className={field} value={provider} onChange={(event) => setProvider(event.target.value)} disabled={!connections.length}>
        <option value="">{connections.length ? "接続を選択" : "登録済みの接続がありません"}</option>
        {connections.map((connection) => <option key={connection.provider_instance_id} value={connection.provider_instance_id}>{connection.display_name}</option>)}
      </select>
    </label>
    {selectedConnection && <>
      <ProviderReadiness connection={selectedConnection} />
      {!manualModel && <CatalogModelPicker models={catalogModels} value={model} query={query}
        onChange={(next) => { setModel(next); setMessage(""); }} onQueryChange={setQuery} />}
      <details>
        <summary className="cursor-pointer text-xs text-zinc-400">カスタムモデル・一覧にないモデル</summary>
        {catalogModels.length > 0 && <label className="mt-2 flex items-center gap-2 text-xs">
          <input type="checkbox" checked={manual} onChange={(event) => { setManual(event.target.checked); setModel(""); }} />モデルIDを指定する
        </label>}
        {manualModel && <label className="mt-2 grid gap-1 text-xs">モデルID
          <input className={field} value={model} autoCorrect="off" autoCapitalize="none" spellCheck={false}
            onChange={(event) => setModel(event.target.value)} placeholder="ProviderのモデルID" />
        </label>}
      </details>
    </>}
    <ModelRouteErrorNotices
      connectionsError={connectionsError}
      saveError={saveError}
    />
    <button type="button" disabled={busy || !validModel || !selectedConnection || providerRegistryRevision === null} onClick={() => void save()} className="rounded border border-zinc-600 px-3 py-2 text-sm disabled:opacity-50">{busy ? "保存結果を確認中" : "このモデルを使う"}</button>
    {message && <p role="status" className="text-xs text-zinc-300">{message}</p>}
  </fieldset>;
}
