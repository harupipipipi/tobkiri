import { useCallback, useEffect, useRef, useState } from "react";
import { Globe, Play, Plus, RefreshCw, Square } from "lucide-react";

import { browserNavigationUrl, browserPendingApproval, browserRecord, browserRows, browserText, type BrowserActionRunner, type BrowserResult } from "../../features/browser/browserModel";
import { browserResources } from "../../features/browser/resources/browserResources";
import { browserApprovalViewModel } from "../../lib/approvalPresentation";
import type { BrowserApproval } from "../../lib/browserApproval";
import { cn } from "../../lib/cn";
import { ApprovalDecisionSurface } from "../ApprovalDecisionSurface";
import { ErrorNotice } from "../ErrorNotice";
import { BrowserCookiesPanel } from "./BrowserCookiesPanel";
import { BrowserDevtoolsPanel } from "./BrowserDevtoolsPanel";
import { BrowserExtensionsPanel } from "./BrowserExtensionsPanel";

export type BrowserWorkspaceSnapshot = {
  runtime: BrowserResult;
  profiles: BrowserResult[];
  activeProfileId?: string;
  tabs?: BrowserResult[];
  extensions?: BrowserResult[];
};

const PANELS = [
  { id: "tabs", label: "タブ" },
  { id: "extensions", label: "拡張機能" },
  { id: "cookies", label: "Cookie インポート" },
  { id: "devtools", label: "開発者ツール" },
] as const;
type BrowserPanel = typeof PANELS[number]["id"];
const inputClass = "min-h-9 min-w-0 rounded-lg border border-zinc-700 bg-zinc-950 px-3 text-xs text-zinc-200 disabled:opacity-50";
const buttonClass = "inline-flex min-h-9 items-center justify-center gap-2 rounded-lg border border-zinc-700 px-3 text-xs font-semibold text-zinc-300 hover:bg-zinc-800 disabled:opacity-40";

export function BrowserWorkspace({ initialSnapshot, resources = browserResources }: {
  initialSnapshot?: BrowserWorkspaceSnapshot;
  resources?: typeof browserResources;
}) {
  const [runtime, setRuntime] = useState<BrowserResult>(initialSnapshot?.runtime ?? {});
  const [profiles, setProfiles] = useState(initialSnapshot?.profiles ?? []);
  const [activeProfileId, setActiveProfileId] = useState(initialSnapshot?.activeProfileId ?? "");
  const [selectedProfileId, setSelectedProfileId] = useState("");
  const [tabs, setTabs] = useState(initialSnapshot?.tabs ?? []);
  const [extensions, setExtensions] = useState(initialSnapshot?.extensions ?? []);
  const [selectedTabId, setSelectedTabId] = useState("");
  const [newTabDraft, setNewTabDraft] = useState(false);
  const [url, setUrl] = useState("");
  const [profileLabel, setProfileLabel] = useState("");
  const [panel, setPanel] = useState<BrowserPanel>("tabs");
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [status, setStatus] = useState<string | null>(null);
  const [pendingApproval, setPendingApproval] = useState<BrowserApproval | null>(null);
  const [inspection, setInspection] = useState<BrowserResult | null>(null);
  const [evaluation, setEvaluation] = useState<BrowserResult | null>(null);
  const [capture, setCapture] = useState<BrowserResult | null>(null);
  const [receipt, setReceipt] = useState("");
  const loadSequence = useRef(0);
  const actionInFlight = useRef(false);
  const mounted = useRef(true);
  const profileId = selectedProfileId || activeProfileId || browserText(runtime.profile_id) || browserText(profiles[0]?.profile_id);
  const tabId = newTabDraft ? "" : tabs.some((tab) => browserText(tab.tab_id) === selectedTabId)
    ? selectedTabId
    : browserText(tabs[0]?.tab_id);
  const selectedTab = tabs.find((tab) => browserText(tab.tab_id) === tabId);
  const running = runtime.running === true;
  const locked = loading || busy;
  const runtimeAvailable = runtime.executable_available !== false && runtime.websocket_available !== false && runtime.supported !== false;

  const refresh = useCallback(async () => {
    const sequence = ++loadSequence.current;
    setLoading(true);
    const errors: string[] = [];
    try {
      const initial = await Promise.allSettled([
        resources.run("browser.runtime.status", profileId ? { profile_id: profileId } : {}),
        resources.run("browser.profiles.list"),
      ]);
      if (!mounted.current || sequence !== loadSequence.current) return;
      let nextRuntime: BrowserResult | null = null;
      let nextProfileId = profileId;
      if (initial[0].status === "fulfilled") { nextRuntime = initial[0].value; setRuntime(nextRuntime); }
      else errors.push(initial[0].reason instanceof Error ? initial[0].reason.message : String(initial[0].reason));
      if (initial[1].status === "fulfilled") {
        const nextProfiles = browserRows(initial[1].value.profiles);
        setProfiles(nextProfiles);
        const nextActiveId = browserText(initial[1].value.active_profile_id);
        setActiveProfileId(nextActiveId);
        nextProfileId ||= nextActiveId || browserText(nextProfiles[0]?.profile_id);
      } else errors.push(initial[1].reason instanceof Error ? initial[1].reason.message : String(initial[1].reason));
      const details = await Promise.allSettled([
        nextRuntime?.running === true
          ? resources.run("browser.tabs", nextProfileId ? { profile_id: nextProfileId } : {})
          : Promise.resolve<BrowserResult>({ tabs: [] }),
        nextProfileId
          ? resources.run("browser.extensions.list", { profile_id: nextProfileId })
          : Promise.resolve<BrowserResult>({ extensions: [] }),
      ]);
      if (!mounted.current || sequence !== loadSequence.current) return;
      if (details[0].status === "fulfilled") setTabs(browserRows(details[0].value.tabs));
      else { setTabs([]); errors.push(details[0].reason instanceof Error ? details[0].reason.message : String(details[0].reason)); }
      if (details[1].status === "fulfilled") setExtensions(browserRows(details[1].value.extensions));
      else { setExtensions([]); errors.push(details[1].reason instanceof Error ? details[1].reason.message : String(details[1].reason)); }
      setError(errors.length ? [...new Set(errors)].join("\n") : null);
    } finally {
      if (mounted.current && sequence === loadSequence.current) setLoading(false);
    }
  }, [profileId, resources]);

  useEffect(() => {
    mounted.current = true;
    void refresh();
    return () => { mounted.current = false; loadSequence.current += 1; };
  }, [refresh]);

  useEffect(() => {
    setUrl(browserText(selectedTab?.url));
  }, [profileId, tabId, selectedTab?.url]);

  useEffect(() => {
    setInspection(null);
    setEvaluation(null);
    setCapture(null);
  }, [profileId, tabId]);

  const run: BrowserActionRunner = async (action, payload = {}) => {
    if (actionInFlight.current) return null;
    actionInFlight.current = true;
    setBusy(true);
    setError(null);
    setStatus(null);
    setPendingApproval(null);
    try {
      const result = await resources.run(action, payload);
      if (!mounted.current) return null;
      const approval = browserPendingApproval(result, action, payload);
      if (approval) { setPendingApproval(approval); return null; }
      if (action === "browser.devtools.inspect") setInspection(result);
      else if (action === "browser.devtools.evaluate") setEvaluation(result);
      else if (action === "browser.network.capture") setCapture(result);
      else {
        if (action === "browser.profile.create") {
          setSelectedProfileId(browserText(result.profile_id ?? browserRecord(result.profile)?.profile_id ?? payload.profile_id));
          setProfileLabel("");
        }
        if (action === "browser.open_url" || action === "browser.select_tab") {
          setSelectedTabId(browserText(result.tab_id ?? payload.tab_id, tabId));
          setNewTabDraft(false);
        }
        await refresh();
      }
      if (!mounted.current) return null;
      setReceipt(`${action}:${Date.now()}`);
      setStatus(browserText(result.message, action === "browser.cookies.import" && result.applied_to_browser === false
        ? "Cookie を保存しました。次回ブラウザ起動時に反映されます。"
        : "ブラウザ操作を完了しました。"));
      return result;
    } catch (err) {
      if (mounted.current) setError(err instanceof Error ? err.message : String(err));
      return null;
    } finally {
      actionInFlight.current = false;
      if (mounted.current) setBusy(false);
    }
  };

  const navigate = () => {
    try {
      const target = browserNavigationUrl(url);
      void run("browser.open_url", { profile_id: profileId, url: target, ...(tabId ? { tab_id: tabId } : {}) });
    } catch (err) { setError(err instanceof Error ? err.message : String(err)); }
  };

  return (
    <main aria-label="Tobkiri Browser" className="flex min-h-0 min-w-0 flex-1 flex-col bg-[var(--rumi-surface-base)]">
      <header className="space-y-3 border-b border-zinc-800 p-4">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0"><h1 className="flex items-center gap-2 text-base font-semibold text-zinc-100"><Globe size={18} />Tobkiri Browser</h1><p className="mt-1 text-xs leading-5 text-zinc-400">モデルと共有する専用ブラウザ。プロファイルごとにログイン状態と拡張機能を保存します。</p></div>
          <button type="button" disabled={locked} onClick={() => void refresh()} className={buttonClass} aria-label="ブラウザの状態を更新"><RefreshCw size={14} className={cn(loading && "animate-spin")} />更新</button>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <label className="flex min-w-0 items-center gap-2 text-xs text-zinc-400">プロファイル<select aria-label="ブラウザプロファイル" value={profileId} disabled={locked} onChange={(event) => { setSelectedProfileId(event.target.value); setSelectedTabId(""); setNewTabDraft(false); setStatus(null); setPendingApproval(null); }} className={inputClass}>{profiles.map((profile) => <option key={browserText(profile.profile_id)} value={browserText(profile.profile_id)}>{browserText(profile.label, browserText(profile.profile_id))}</option>)}{!profiles.length && <option value="">プロファイルを作成してください</option>}</select></label>
          <span role="status" className={cn("rounded-md px-2 py-1 text-xs", running ? "bg-emerald-500/10 text-emerald-300" : "bg-zinc-800 text-zinc-400")}>{running ? "起動中" : "停止中"}</span>
          <button type="button" disabled={locked || !profileId || !runtimeAvailable} onClick={() => void run(running ? "browser.runtime.stop" : "browser.runtime.start", { profile_id: profileId })} className={buttonClass}>{running ? <Square size={13} /> : <Play size={13} />}{running ? "停止" : "ブラウザを起動"}</button>
          <form onSubmit={(event) => { event.preventDefault(); if (profileLabel.trim() && !locked) void run("browser.profile.create", { profile_id: `browser-${crypto.randomUUID().slice(0, 12)}`, label: profileLabel.trim() }); }} className="flex min-w-0 items-center gap-2"><input aria-label="新しいブラウザプロファイル名" value={profileLabel} disabled={locked} onChange={(event) => setProfileLabel(event.target.value)} placeholder="新しいプロファイル名" maxLength={80} className={inputClass} /><button type="submit" disabled={locked || !profileLabel.trim()} className={buttonClass}><Plus size={13} />作成</button></form>
        </div>
        {!running && <p className="text-xs leading-5 text-zinc-500">起動すると専用プロファイルの Chromium ウィンドウが開きます。モデルも同じブラウザをツールから操作できます。</p>}
        {Boolean(runtime.message) && <p className="text-xs leading-5 text-zinc-400">{browserText(runtime.message)}</p>}
        {!runtimeAvailable && <p role="status" className="text-xs leading-5 text-amber-200">この端末でブラウザを起動する準備ができていません。ブラウザ実行環境を設定してから状態を更新してください。</p>}
        {error && <ErrorNotice message={error} copyLabel="ブラウザのエラーをコピー" />}
        {status && <p role="status" className="text-xs text-zinc-300">{status}</p>}
        {busy && <p role="status" className="text-xs text-zinc-400">ブラウザ操作を実行しています…</p>}
        {pendingApproval && <div className="space-y-2"><ApprovalDecisionSurface approval={{ ...browserApprovalViewModel(pendingApproval), trustedWindowRequired: true }} /><p className="text-xs leading-5 text-zinc-400">Tobkiri Launcher のプロファイル権限を確認し、許可後に操作を再試行してください。</p><button type="button" className={buttonClass} onClick={() => setPendingApproval(null)}>閉じる</button></div>}
      </header>
      <nav aria-label="Browser panels" className="flex shrink-0 gap-1 overflow-x-auto border-b border-zinc-800 px-3 py-2">{PANELS.map((item) => <button key={item.id} type="button" aria-current={panel === item.id ? "page" : undefined} onClick={() => setPanel(item.id)} className={cn("min-h-9 shrink-0 rounded-lg px-3 text-xs font-semibold", panel === item.id ? "bg-zinc-800 text-zinc-100" : "text-zinc-500 hover:bg-zinc-900 hover:text-zinc-200")}>{item.label}</button>)}</nav>
      <div className="min-h-0 flex-1 overflow-y-auto p-4">
        {panel === "tabs" && <section aria-label="Browser tabs" className="space-y-4">
          <form onSubmit={(event) => { event.preventDefault(); navigate(); }} className="flex gap-2"><label className="min-w-0 flex-1"><span className="sr-only">ブラウザの URL</span><input type="url" aria-label="ブラウザの URL" value={url} disabled={locked || !running} onChange={(event) => setUrl(event.target.value)} placeholder="https://example.com" className={cn(inputClass, "w-full")} /></label><button type="submit" disabled={locked || !running || !url.trim()} className={buttonClass}>開く</button><button type="button" disabled={locked || !running || !tabId} onClick={() => { setSelectedTabId(""); setNewTabDraft(true); setUrl(""); }} className={buttonClass}>新しいタブ</button></form>
          <ul className="grid gap-2 sm:grid-cols-2 xl:grid-cols-3">{tabs.map((tab) => <li key={browserText(tab.tab_id)}><button type="button" aria-pressed={browserText(tab.tab_id) === tabId} disabled={locked || !running} onClick={() => void run("browser.select_tab", { profile_id: profileId, tab_id: tab.tab_id })} className={cn("w-full space-y-1 rounded-xl border p-4 text-left", browserText(tab.tab_id) === tabId ? "border-sky-500/45 bg-sky-500/5" : "border-zinc-800 bg-zinc-950/45 hover:border-zinc-700")}><p className="truncate text-sm font-semibold text-zinc-200">{browserText(tab.title, "Untitled tab")}</p><p className="truncate text-xs text-zinc-500">{browserText(tab.url, "about:blank")}</p></button></li>)}</ul>
          {!tabs.length && <div className="rounded-xl border border-dashed border-zinc-800 p-8 text-center text-xs leading-6 text-zinc-500">{running ? "URL を入力してページを開いてください。" : "ブラウザを起動すると、開いているタブがここに表示されます。"}</div>}
        </section>}
        {panel === "extensions" && <BrowserExtensionsPanel key={profileId} extensions={extensions} profileId={profileId} disabled={locked} receipt={receipt} onAction={run} />}
        {panel === "cookies" && <BrowserCookiesPanel key={profileId} profileId={profileId} disabled={locked} receipt={receipt} onAction={run} />}
        {panel === "devtools" && <BrowserDevtoolsPanel key={`${profileId}:${tabId}`} profileId={profileId} tabId={tabId} disabled={locked || !running} inspection={inspection} evaluation={evaluation} capture={capture} onAction={run} />}
      </div>
    </main>
  );
}
