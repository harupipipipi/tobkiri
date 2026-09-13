import { useState } from "react";
import { settingsApiResources } from "../settings/resources/settingsApiResources";

/** Configure a model independently of credential creation or rotation. */
export function ModelRouteSetup() {
  const [id, setId] = useState("");
  const [model, setModel] = useState("");
  const [provider, setProvider] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const save = async () => {
    setBusy(true);
    setMessage("");
    try {
      await settingsApiResources.createModelProfile({
        model_profile_id: id.trim(), model_id: model.trim(),
        provider_instance_id: provider.trim(), display_name: id.trim(),
      });
      setMessage("モデル設定を保存しました。モデル一覧から選択してください。Provider到達確認はまだ行っていません。");
      window.dispatchEvent(new Event("tobkiri-model-profiles-changed"));
    } catch {
      setMessage("モデル設定を確認できません。同じ入力で保存を押すと既存設定を照合します。APIキーは再送しません。");
    } finally {
      setBusy(false);
    }
  };
  const field = "rounded border border-zinc-700 bg-zinc-950 px-2 py-2 text-sm text-zinc-100";
  return <fieldset disabled={busy} className="mt-3 grid gap-2 rounded-lg border border-zinc-700 p-3">
    <legend className="text-sm text-zinc-200">モデルルート作成（キー保存とは別操作）</legend>
    <label className="grid gap-1 text-xs">モデル設定ID<input className={field} value={id} onChange={(event) => setId(event.target.value)} placeholder="daily" /></label>
    <label className="grid gap-1 text-xs">Provider接続ID<input className={field} value={provider} onChange={(event) => setProvider(event.target.value)} placeholder="provider.deepseek.main" /></label>
    <label className="grid gap-1 text-xs">モデルID<input className={field} value={model} onChange={(event) => setModel(event.target.value)} placeholder="ProviderのモデルID" /></label>
    <button type="button" disabled={busy || !id.trim() || !model.trim() || !provider.trim()} onClick={() => void save()} className="rounded border border-zinc-600 px-3 py-2 text-sm disabled:opacity-50">{busy ? "保存結果を確認中" : "モデルルートを保存"}</button>
    {message && <p role="status" className="text-xs text-zinc-300">{message}</p>}
  </fieldset>;
}
