import { useCallback, useEffect, useRef, useState } from "react";

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

export function ProviderReadiness({
  connection,
}: {
  connection: RegisteredProviderConnection;
}) {
  const credential = connection.credential_status === "configured"
    ? "資格情報: 設定済み"
    : connection.credential_status === "not_required"
      ? "資格情報: 不要（ローカルモデル）"
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
  const [connectionsLoading, setConnectionsLoading] = useState(false);
  const [retryRequired, setRetryRequired] = useState(false);
  const [retryConfirmed, setRetryConfirmed] = useState(false);
  const active = useRef(false);
  const connectionsGeneration = useRef(0);
  const saveInFlight = useRef(false);

  const loadConnections = useCallback(async () => {
    const generation = ++connectionsGeneration.current;
    setConnectionsLoading(true);
    setProviderRegistryRevision(null);
    setRetryConfirmed(false);
    try {
      const snapshot = await settingsApiResources.listProviderConnections();
      if (!active.current || generation !== connectionsGeneration.current) return;
      setConnections(snapshot.connections);
      setProviderRegistryRevision(snapshot.registry_revision);
      setConnectionsError("");
      setProvider((current) => (
        snapshot.connections.some((connection) => connection.provider_instance_id === current)
          ? current
          : ""
      ));
    } catch {
      if (!active.current || generation !== connectionsGeneration.current) return;
      setConnections([]);
      setProviderRegistryRevision(null);
      setConnectionsError("Provider接続一覧を確認できません。接続一覧を更新してから設定してください。");
    } finally {
      if (active.current && generation === connectionsGeneration.current) {
        setConnectionsLoading(false);
      }
    }
  }, []);

  useEffect(() => {
    active.current = true;
    void loadConnections();
    const changed = () => { void loadConnections(); };
    window.addEventListener("tobkiri-provider-connections-changed", changed);
    return () => {
      active.current = false;
      ++connectionsGeneration.current;
      window.removeEventListener("tobkiri-provider-connections-changed", changed);
    };
  }, [loadConnections]);

  const selectedConnection = connections.find(
    (connection) => connection.provider_instance_id === provider,
  );
  const save = async () => {
    if (saveInFlight.current || busy || connectionsLoading || !selectedConnection || providerRegistryRevision === null
      || (retryRequired && !retryConfirmed)) return;
    saveInFlight.current = true;
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
      if (!active.current) return;
      setRetryRequired(false);
      setRetryConfirmed(false);
      setMessage("モデル設定を保存しました。モデル一覧から選択してください。Provider到達確認はまだ行っていません。");
      window.dispatchEvent(new Event("tobkiri-model-profiles-changed"));
    } catch {
      if (!active.current) return;
      setRetryRequired(true);
      setRetryConfirmed(false);
      setSaveError("保存を確認できませんでした。入力したIDは保持しています。最新の接続を確認し、再送に同意してから保存し直してください。APIキーは再送しません。");
      await loadConnections();
    } finally {
      saveInFlight.current = false;
      if (active.current) setBusy(false);
    }
  };
  const field = "rounded border border-zinc-700 bg-zinc-950 px-2 py-2 text-sm text-zinc-100";
  return <fieldset disabled={busy} className="mt-3 grid gap-2 rounded-lg border border-zinc-700 p-3">
    <legend className="text-sm text-zinc-200">モデルルート作成（キー保存とは別操作）</legend>
    <label className="grid gap-1 text-xs">モデル設定ID<input autoCorrect="off" autoCapitalize="none" spellCheck={false} className={field} value={id} onChange={(event) => { setId(event.target.value); setRetryConfirmed(false); }} placeholder="daily" /></label>
    <label className="grid gap-1 text-xs">Provider接続ID（登録済みのみ）
      <select aria-label="Provider connection ID" className={field} value={provider} onChange={(event) => { setProvider(event.target.value); setRetryConfirmed(false); }} disabled={connectionsLoading || !connections.length}>
        <option value="">{connections.length ? "接続を選択" : "登録済みの接続がありません"}</option>
        {connections.map((connection) => <option key={connection.provider_instance_id} value={connection.provider_instance_id}>{connection.display_name} — {connection.provider_instance_id}</option>)}
      </select>
    </label>
    {selectedConnection && <>
      <p className="font-mono text-xs text-zinc-400">使用する接続ID: {selectedConnection.provider_instance_id}</p>
      <ProviderReadiness connection={selectedConnection} />
    </>}
    <button type="button" disabled={connectionsLoading} onClick={() => void loadConnections()} className="rounded border border-zinc-600 px-3 py-2 text-sm disabled:opacity-50">{connectionsLoading ? "Provider接続一覧を更新中" : "Provider接続一覧を更新"}</button>
    <ModelRouteErrorNotices
      connectionsError={connectionsError}
      saveError={saveError}
    />
    <label className="grid gap-1 text-xs">モデルID<input autoCorrect="off" autoCapitalize="none" spellCheck={false} className={field} value={model} onChange={(event) => { setModel(event.target.value); setRetryConfirmed(false); }} placeholder="ProviderのモデルID" /></label>
    {retryRequired && <>
      {!connectionsLoading && !connectionsError && !selectedConnection && <p role="status" className="text-xs text-zinc-300">選択したProvider接続は削除または無効化された可能性があります。登録済みの接続を選択し直してください。</p>}
      <label className="flex gap-2 text-xs"><input type="checkbox" checked={retryConfirmed} disabled={connectionsLoading || !selectedConnection || providerRegistryRevision === null} onChange={(event) => setRetryConfirmed(event.target.checked)} />最新のProvider接続を確認しました。入力したモデルルートの再送に同意します。</label>
    </>}
    <button type="button" disabled={busy || connectionsLoading || !id.trim() || !model.trim() || !selectedConnection || providerRegistryRevision === null || (retryRequired && !retryConfirmed)} onClick={() => void save()} className="rounded border border-zinc-600 px-3 py-2 text-sm disabled:opacity-50">{busy ? "保存結果を確認中" : retryRequired ? "確認したモデルルートを再送" : "モデルルートを保存"}</button>
    {message && <p role="status" className="text-xs text-zinc-300">{message}</p>}
  </fieldset>;
}
