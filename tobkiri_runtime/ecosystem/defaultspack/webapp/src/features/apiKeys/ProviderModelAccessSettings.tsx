import { useEffect, useId, useReducer, useRef, useState } from "react";
import { ChevronDown } from "lucide-react";
import {
  initialModelAccessDisclosureState,
  modelAccessDisclosureReducer,
  modelAccessPolicySummary,
} from "./modelAccessDisclosure";
import {
  MODEL_ACCESS_VERSION,
  OPENROUTER_FILTER_REVISION,
  normalizeModelAccess,
  toggleAllowedModel,
  type ModelAccessPolicy,
  type NativeRouting,
} from "./modelAccessPolicy";
import type {
  ModelAccessCatalog,
  ModelAccessScope,
  ModelAccessSnapshot,
  ProviderModelAccessResources,
} from "./resources/providerModelAccessResources";

const emptyPolicy = (): ModelAccessPolicy => ({ version: MODEL_ACCESS_VERSION, mode: "explicit", model_ids: [] });
const inputClass = "rounded border border-zinc-700 bg-zinc-950 px-2 py-1 text-sm";
const split = (text: string) => text.split(",").map((value) => value.trim()).filter(Boolean);

/** Edit restrictions for one verified Host profile and opaque saved connection. */
export function ProviderModelAccessSettings({
  scope,
  resources,
  onSnapshot,
}: {
  scope: ModelAccessScope;
  resources: ProviderModelAccessResources;
  onSnapshot?: (snapshot: ModelAccessSnapshot | null) => void;
}) {
  const group = useId();
  const [saved, setSaved] = useState<ModelAccessSnapshot | null>(null);
  const [draft, setDraft] = useState<ModelAccessPolicy>(emptyPolicy);
  const [catalog, setCatalog] = useState<ModelAccessCatalog>({ ...scope, models: [], status: "unavailable" });
  const [query, setQuery] = useState("");
  const [routingText, setRoutingText] = useState<Record<string, string>>({});
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [reload, setReload] = useState(0);
  const [disclosure, dispatchDisclosure] = useReducer(
    modelAccessDisclosureReducer,
    initialModelAccessDisclosureState,
  );
  const epoch = useRef(0);
  useEffect(() => {
    const ticket = ++epoch.current;
    onSnapshot?.(null);
    setSaved(null); setDraft(emptyPolicy()); setCatalog({ ...scope, models: [], status: "unavailable" });
    setNotice(""); setQuery(""); setRoutingText({}); setLoading(true); setBusy(false);
    dispatchDisclosure({ type: "loading" });
    void resources.load(scope).then(async (snapshot) => {
      if (ticket !== epoch.current) return;
      setSaved(snapshot); setDraft(snapshot.model_access ?? emptyPolicy()); onSnapshot?.(snapshot);
      dispatchDisclosure({ type: "loaded" });
      try {
        const page = await resources.catalog(scope, snapshot.model_access?.native_filters?.discovery);
        if (ticket === epoch.current) setCatalog(page);
      } catch {
        if (ticket === epoch.current) setNotice("モデル一覧を取得できません。保存済みの個別指定は保持しています。");
      }
    }).catch(() => {
      if (ticket === epoch.current) {
        setSaved(null); onSnapshot?.(null);
        setNotice("この接続のモデル許可を確認できません。再読み込みしてから設定してください。");
      }
    }).finally(() => { if (ticket === epoch.current) setLoading(false); });
    return () => { ++epoch.current; };
  }, [scope.profile_id, scope.provider_instance_id, resources, onSnapshot, reload]);

  const nativeAvailable = saved?.native_capability === OPENROUTER_FILTER_REVISION;
  const routing = draft.native_filters?.routing ?? {};
  const discovery = draft.native_filters?.discovery ?? {};
  const updateNative = (part: "discovery" | "routing", key: string, value: unknown) => {
    if (!nativeAvailable) return;
    setDraft((current) => {
      const native = current.native_filters ?? { revision: OPENROUTER_FILTER_REVISION, discovery: {}, routing: {} };
      const next: Record<string, unknown> = { ...native[part], [key]: value };
      if (value === undefined) delete next[key];
      return { ...current, native_filters: { ...native, [part]: next } };
    });
  };
  const save = async () => {
    if (!saved || busy) return;
    let policy: ModelAccessPolicy;
    try {
      policy = normalizeModelAccess(draft, saved.native_capability);
    } catch {
      setNotice("入力を確認してください。価格上限は0以上・小数6桁以内のUSD値で指定してください。保存は送信していません。");
      return;
    }
    const ticket = epoch.current;
    setBusy(true); setNotice("");
    try {
      const result = await resources.save(saved, policy);
      if (ticket !== epoch.current) return;
      setSaved(result); setDraft(result.model_access ?? emptyPolicy()); setRoutingText({}); onSnapshot?.(result);
      setNotice("この接続のモデル許可を保存しました。");
      window.dispatchEvent(new Event("tobkiri-provider-connections-changed"));
    } catch {
      if (ticket === epoch.current) {
        setSaved(null); onSnapshot?.(null);
        setNotice("保存結果を確認できません。承認画面を確認し、保存済みの状態を再確認してください。自動再送はしません。");
      }
    } finally { if (ticket === epoch.current) setBusy(false); }
  };
  const refreshCatalog = async () => {
    if (!saved || busy) return;
    const ticket = epoch.current;
    setBusy(true); setNotice("");
    try {
      const normalized = normalizeModelAccess(draft, saved.native_capability);
      const page = await resources.catalog(scope, normalized.native_filters?.discovery);
      if (ticket !== epoch.current) return;
      setCatalog(page);
      setNotice(page.status === "live" ? (page.models.length ? "公式条件で一覧を更新しました。" : "公式条件に合うモデルがありません。") : "公式のモデル一覧は現在取得できません。");
    } catch { if (ticket === epoch.current) setNotice("公式条件またはモデル一覧を確認できません。個別指定は保持しています。"); }
    finally { if (ticket === epoch.current) setBusy(false); }
  };
  const visible = catalog.models.filter((model) => `${model.display_name} ${model.model_id}`.toLowerCase().includes(query.toLowerCase()));
  const missing = draft.model_ids.filter((id) => !catalog.models.some((model) => model.model_id === id));
  let invalid = "";
  try { normalizeModelAccess(draft, saved?.native_capability ?? null); }
  catch (error) { invalid = error instanceof Error ? error.message : "設定が無効です。"; }

  const policySummary = modelAccessPolicySummary(loading, saved?.model_access);
  const needsModelSelection = saved?.model_access?.mode === "explicit"
    && saved.model_access.model_ids.length === 0;

  return <section aria-label="このAPI接続で使えるモデル" className="border-y border-white/[0.07]">
    <details open={disclosure.settingsOpen} onToggle={(event) => dispatchDisclosure({ type: "toggle-settings", open: event.currentTarget.open })}>
      <summary className="group flex cursor-pointer list-none items-center justify-between gap-3 px-3 py-2.5 text-xs text-zinc-300 [&::-webkit-details-marker]:hidden">
        <span className="font-medium">モデル利用範囲</span>
        <span className="flex items-center gap-1.5 text-right text-zinc-500">{policySummary}<ChevronDown size={14} className="shrink-0 transition-transform group-open:rotate-180" aria-hidden="true" /></span>
      </summary>
      <div className="grid gap-3 border-t border-white/[0.07] px-1 py-3">
    <p className="text-xs leading-5 text-zinc-500">この接続で選べるモデルを管理します。ProviderやProfile側の制限も引き続き適用されます。</p>
    {loading ? <p role="status">設定を読み込み中…</p> : !saved ? <>
      <p role="alert">{notice}</p>
      <button type="button" className="justify-self-start rounded border border-zinc-700 px-3 py-1.5 text-sm" onClick={() => setReload((current) => current + 1)}>保存済みの状態を再確認</button>
    </> : <>
      {saved.model_access === null && <p className="text-xs text-zinc-400">この接続には追加のモデル制限を設定していません。Provider・Profile・Nativeの許可範囲が適用されます。</p>}
      <fieldset disabled={busy} className="grid gap-2 text-sm">
        <legend className="sr-only">モデル許可モード</legend>
        <label><input type="radio" name={group} checked={draft.mode === "all"} onChange={() => setDraft((current) => ({ ...current, mode: "all", model_ids: [] }))} /> 全モデルを許可</label>
        <p className="text-xs text-zinc-400">このProviderの正規カタログに今後追加されるモデルも対象になります。</p>
        <label><input type="radio" name={group} checked={draft.mode === "explicit"} onChange={() => setDraft((current) => ({ ...current, mode: "explicit" }))} /> モデルを個別に指定</label>
        {draft.mode === "explicit" && <>
          <p className="text-xs text-zinc-400">選択済み: {draft.model_ids.length}件{!draft.model_ids.length && "（この内容で保存すると、モデルを利用できません）"}</p>
          {catalog.status !== "live" && <p role="status" className="text-xs">カタログは未取得です。保存済みの指定は削除しません。</p>}
          <details open={disclosure.modelListOpen} onToggle={(event) => dispatchDisclosure({ type: "toggle-model-list", open: event.currentTarget.open })} className="rounded-lg border border-white/[0.07]">
            <summary className="group flex cursor-pointer list-none items-center justify-between gap-2 px-2.5 py-2 text-xs text-zinc-300 [&::-webkit-details-marker]:hidden"><span>{draft.model_ids.length ? `許可モデルを確認・編集 (${draft.model_ids.length}件)` : "許可するモデルを選択"}</span><ChevronDown size={13} className="shrink-0 text-zinc-500 transition-transform group-open:rotate-180" aria-hidden="true" /></summary>
            <div className="grid gap-2 border-t border-white/[0.06] p-2.5">
              <label className="grid gap-1 text-xs text-zinc-400">モデルを検索<input className={inputClass} value={query} onChange={(event) => setQuery(event.target.value)} placeholder="名前またはモデルID" /></label>
              <div className="max-h-48 overflow-auto">
            {visible.map((model) => <label className="block py-1" key={model.model_id}><input type="checkbox" checked={draft.model_ids.includes(model.model_id)} onChange={() => setDraft((current) => toggleAllowedModel(current, model.model_id))} /> {model.display_name} <span className="text-xs text-zinc-400">{model.model_id}</span></label>)}
            {missing.map((id) => <label className="block py-1" key={id}><input type="checkbox" checked onChange={() => setDraft((current) => toggleAllowedModel(current, id))} /> {id} <span className="text-xs text-zinc-400">一覧外・利用可否未確認</span></label>)}
              </div>
          </div>
          </details>
        </>}
      </fieldset>
      {nativeAvailable && <details><summary>OpenRouter公式フィルター</summary>
        <fieldset disabled={busy} className="mt-2 grid gap-2 text-xs">
          <legend>モデル一覧の公式条件</legend>
          <p className="text-zinc-400">公開Models APIで一覧を取得する条件です。アカウントの利用許可や生成経路を変更する条件ではありません。</p>
          <label className="grid gap-1">出力形式<select className={inputClass} value={discovery.output_modalities?.[0] ?? "all"} onChange={(event) => updateNative("discovery", "output_modalities", [event.target.value])}>{["all", "text", "image", "audio", "embeddings", "video"].map((id) => <option key={id} value={id}>{id === "all" ? "全形式" : id}</option>)}</select></label>
          <label className="grid gap-1">用途カテゴリ<select className={inputClass} value={discovery.category ?? ""} onChange={(event) => updateNative("discovery", "category", event.target.value || undefined)}><option value="">指定なし</option>{["programming", "roleplay", "marketing", "technology", "science", "translation", "legal", "finance", "health", "trivia", "academia"].map((id) => <option key={id} value={id}>{id}</option>)}</select></label>
          <button type="button" onClick={() => void refreshCatalog()}>公式条件で一覧を更新</button>
        </fieldset>
        <fieldset disabled={busy} className="mt-3 grid gap-2 text-xs"><legend>生成時の公式経路条件</legend>
          <p className="text-zinc-400">OpenRouter内部の実行事業者を制限します。条件に合う経路がなければエラーになります。条件外への自動切替は行いません。</p>
          {(["only", "ignore", "order", "quantizations"] as const).map((key) => <label className="grid gap-1" key={key}>{({ only: "使う実行事業者ID（カンマ区切り）", ignore: "除外する実行事業者ID（カンマ区切り）", order: "実行事業者の優先順（カンマ区切り）", quantizations: "量子化形式（fp16、bf16など、カンマ区切り）" })[key]}<input className={inputClass} value={routingText[key] ?? routing[key]?.join(", ") ?? ""} onChange={(event) => { setRoutingText((current) => ({ ...current, [key]: event.target.value })); const values = split(event.target.value); updateNative("routing", key, values.length ? values : undefined); }} /></label>)}
          {(["require_parameters", "zdr"] as const).map((key) => <label key={key}><input type="checkbox" checked={routing[key] ?? false} onChange={(event) => updateNative("routing", key, event.target.checked || undefined)} /> {key === "zdr" ? "データ保持ゼロの経路のみ" : "指定した生成パラメーターに対応する経路のみ"}</label>)}
          <label>データ収集<select className={inputClass} value={routing.data_collection ?? ""} onChange={(event) => updateNative("routing", "data_collection", event.target.value || undefined)}><option value="">指定なし</option><option value="deny">収集を許可しない経路</option><option value="allow">収集を許可</option></select></label>
          <label>経路の優先条件<select className={inputClass} value={routing.sort ?? ""} onChange={(event) => updateNative("routing", "sort", event.target.value || undefined)}><option value="">Providerの既定</option><option value="price">価格</option><option value="latency">応答の速さ</option><option value="throughput">生成速度</option></select></label>
          {(["prompt", "completion"] as const).map((unit) => <label className="grid gap-1" key={unit}>{unit === "prompt" ? "入力" : "出力"}価格上限（USD / 100万トークン）<input type="number" min="0" step="any" className={inputClass} value={routing.max_price?.[unit] ?? ""} onChange={(event) => { const prices: NonNullable<NativeRouting["max_price"]> = { ...routing.max_price }; if (event.target.value === "") delete prices[unit]; else prices[unit] = event.target.value; updateNative("routing", "max_price", Object.keys(prices).length ? prices : undefined); }} /></label>)}
          <button type="button" onClick={() => { setRoutingText({}); setDraft((current) => { const next = { ...current }; delete next.native_filters; return next; }); }}>公式フィルターを解除</button>
        </fieldset>
      </details>}
      {invalid && <p role="alert">{invalid}</p>}
      <div className="flex flex-wrap items-center gap-2"><button type="button" className="rounded-md border border-white/15 bg-white/[0.06] px-3 py-1.5 text-xs text-zinc-200 hover:bg-white/[0.1] disabled:opacity-40" disabled={busy || Boolean(invalid)} onClick={() => void save()}>利用範囲を保存</button><button type="button" className="rounded-md px-2.5 py-1.5 text-xs text-zinc-500 hover:text-zinc-300 disabled:opacity-40" disabled={busy} onClick={() => { setDraft(saved.model_access ?? emptyPolicy()); setRoutingText({}); setNotice(""); }}>変更を取消</button></div>
      {notice && <p role="status" className="text-xs text-zinc-400">{notice}</p>}
    </>}
      </div>
    </details>
    {!disclosure.settingsOpen && !loading && (!saved || needsModelSelection) && <button type="button" onClick={() => dispatchDisclosure({ type: "toggle-settings", open: true })} className="mx-3 mb-3 rounded-md border border-amber-300/20 bg-amber-300/[0.05] px-2.5 py-1.5 text-left text-xs text-amber-100 hover:bg-amber-300/[0.09]">{needsModelSelection ? "利用できるモデルが0件です。許可するモデルを選択" : "接続の許可設定を確認"}</button>}
  </section>;
}
