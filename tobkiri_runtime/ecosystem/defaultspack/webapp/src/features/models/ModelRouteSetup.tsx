import { useEffect, useState } from "react";

import { ErrorNotice } from "../../components/ErrorNotice";
import type { RegisteredProviderConnection } from "../../lib/api";
import { settingsApiResources } from "../settings/resources/settingsApiResources";

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

/** Configure a model independently of credential creation or rotation. */
export function ModelRouteSetup() {
  const [id, setId] = useState("");
  const [model, setModel] = useState("");
  const [provider, setProvider] = useState("");
  const [connections, setConnections] = useState<RegisteredProviderConnection[]>([]);
  const [providerRegistryRevision, setProviderRegistryRevision] = useState<number | null>(null);
  const [connectionsError, setConnectionsError] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [saveError, setSaveError] = useState("");

  useEffect(() => {
    let active = true;
    const loadConnections = async () => {
      try {
        const snapshot = await settingsApiResources.listProviderConnections();
        if (!active) return;
        setConnections(snapshot.connections);
        setProviderRegistryRevision(snapshot.registry_revision);
        setConnectionsError("");
        setProvider((current) => (
          snapshot.connections.some((connection) => connection.provider_instance_id === current)
            ? current
            : ""
        ));
      } catch {
        if (!active) return;
        setConnections([]);
        setProviderRegistryRevision(null);
        setProvider("");
        setConnectionsError("Provider接続一覧を確認できません。再読み込みしてから設定してください。");
      }
    };
    void loadConnections();
    window.addEventListener("tobkiri-provider-connections-changed", loadConnections);
    return () => {
      active = false;
      window.removeEventListener("tobkiri-provider-connections-changed", loadConnections);
    };
  }, []);

  const selectedConnection = connections.find(
    (connection) => connection.provider_instance_id === provider,
  );
  const save = async () => {
    if (!selectedConnection || providerRegistryRevision === null) return;
    setBusy(true);
    setMessage("");
    setSaveError("");
    try {
      await settingsApiResources.createModelProfile({
        model_profile_id: id.trim(),
        model_id: model.trim(),
        provider_instance_id: selectedConnection.provider_instance_id,
        display_name: id.trim(),
        provider_registry_revision: providerRegistryRevision,
      });
      setMessage("モデル設定を保存しました。モデル一覧から選択してください。Provider到達確認はまだ行っていません。");
      window.dispatchEvent(new Event("tobkiri-model-profiles-changed"));
    } catch {
      setSaveError("Provider接続またはモデル設定が更新された可能性があります。接続を確認してから保存し直してください。APIキーは再送しません。");
    } finally {
      setBusy(false);
    }
  };
  const field = "rounded border border-zinc-700 bg-zinc-950 px-2 py-2 text-sm text-zinc-100";
  return <fieldset disabled={busy} className="mt-3 grid gap-2 rounded-lg border border-zinc-700 p-3">
    <legend className="text-sm text-zinc-200">モデルルート作成（キー保存とは別操作）</legend>
    <label className="grid gap-1 text-xs">モデル設定ID<input className={field} value={id} onChange={(event) => setId(event.target.value)} placeholder="daily" /></label>
    <label className="grid gap-1 text-xs">Provider接続ID（登録済みのみ）
      <select aria-label="Provider connection ID" className={field} value={provider} onChange={(event) => setProvider(event.target.value)} disabled={!connections.length}>
        <option value="">{connections.length ? "接続を選択" : "登録済みの接続がありません"}</option>
        {connections.map((connection) => <option key={connection.provider_instance_id} value={connection.provider_instance_id}>{connection.display_name} — {connection.provider_instance_id}</option>)}
      </select>
    </label>
    {selectedConnection && <p className="font-mono text-xs text-zinc-400">使用する接続ID: {selectedConnection.provider_instance_id}</p>}
    <ModelRouteErrorNotices
      connectionsError={connectionsError}
      saveError={saveError}
    />
    <label className="grid gap-1 text-xs">モデルID<input className={field} value={model} onChange={(event) => setModel(event.target.value)} placeholder="ProviderのモデルID" /></label>
    <button type="button" disabled={busy || !id.trim() || !model.trim() || !selectedConnection || providerRegistryRevision === null} onClick={() => void save()} className="rounded border border-zinc-600 px-3 py-2 text-sm disabled:opacity-50">{busy ? "保存結果を確認中" : "モデルルートを保存"}</button>
    {message && <p role="status" className="text-xs text-zinc-300">{message}</p>}
  </fieldset>;
}
