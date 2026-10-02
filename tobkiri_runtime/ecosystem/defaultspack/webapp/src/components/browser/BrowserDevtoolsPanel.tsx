import { useState } from "react";
import { Code2, Network, SearchCode } from "lucide-react";

import { browserRows, browserText, type BrowserActionRunner, type BrowserResult } from "../../features/browser/browserModel";

export function BrowserDevtoolsPanel({ profileId, tabId, disabled, inspection, evaluation, capture, onAction }: {
  profileId: string;
  tabId: string;
  disabled: boolean;
  inspection: BrowserResult | null;
  evaluation: BrowserResult | null;
  capture: BrowserResult | null;
  onAction: BrowserActionRunner;
}) {
  const [expression, setExpression] = useState("document.title");
  const [durationMs, setDurationMs] = useState(2000);
  const [reload, setReload] = useState(false);
  const unavailable = disabled || !tabId;
  const requests = browserRows(capture?.requests);
  const consoleEntries = browserRows(capture?.console);

  return (
    <section aria-label="Browser developer tools" className="space-y-5">
      <div>
        <h2 className="flex items-center gap-2 text-sm font-semibold text-zinc-100"><Code2 size={16} />開発者ツール</h2>
        <p className="mt-1 text-xs leading-5 text-zinc-400">選択中のタブの DOM とレイアウトを確認し、JavaScript を実行できます。モデルにも同じ機能をツールとして提供します。</p>
      </div>
      {!tabId && <p role="status" className="rounded-lg border border-zinc-800 p-3 text-xs text-zinc-400">ブラウザを起動して、確認するタブを選択してください。</p>}
      <div className="space-y-3 rounded-xl border border-zinc-800 bg-zinc-950/45 p-4">
        <button type="button" disabled={unavailable} onClick={() => void onAction("browser.devtools.inspect", { profile_id: profileId, tab_id: tabId })} className="flex min-h-9 items-center gap-2 rounded-lg border border-zinc-700 px-3 text-xs font-semibold text-zinc-200 hover:bg-zinc-800 disabled:opacity-40"><SearchCode size={14} />DOM・レイアウトを取得</button>
        {inspection && <pre aria-label="DOM とレイアウト" className="max-h-80 overflow-auto rounded-lg bg-black/40 p-3 text-[11px] text-zinc-300">{JSON.stringify(inspection, null, 2)}</pre>}
        <form onSubmit={(event) => { event.preventDefault(); if (!unavailable && expression.trim()) void onAction("browser.devtools.evaluate", { profile_id: profileId, tab_id: tabId, expression }); }} className="grid gap-2">
          <label className="grid gap-1 text-xs text-zinc-300">JavaScript 式
            <textarea aria-label="JavaScript 式" value={expression} disabled={unavailable} onChange={(event) => setExpression(event.target.value)} rows={3} className="resize-y rounded-lg border border-zinc-700 bg-zinc-950 p-3 font-mono text-xs text-zinc-200" />
          </label>
          <p className="text-xs leading-5 text-zinc-500">JavaScript の実行はページの状態を変更する可能性があります。</p>
          <button type="submit" disabled={unavailable || !expression.trim()} className="min-h-9 justify-self-start rounded-lg bg-zinc-100 px-4 text-xs font-semibold text-zinc-950 hover:bg-white disabled:opacity-40">確認して実行</button>
        </form>
        {evaluation && <pre aria-label="JavaScript の結果" className="max-h-60 overflow-auto rounded-lg bg-black/40 p-3 text-[11px] text-zinc-300">{JSON.stringify(evaluation, null, 2)}</pre>}
      </div>
      <div className="space-y-3 rounded-xl border border-zinc-800 bg-zinc-950/45 p-4">
        <h3 className="flex items-center gap-2 text-sm font-semibold text-zinc-100"><Network size={16} />ネットワーク・Console</h3>
        <p className="text-xs leading-5 text-zinc-400">短い時間の通信と Console のイベントを記録します。URL のクエリ値、認証ヘッダー、通信本文、Console の値は含めません。</p>
        <div className="flex flex-wrap items-center gap-3">
          <label className="flex items-center gap-2 text-xs text-zinc-300">記録時間
            <select aria-label="ネットワークの記録時間" value={durationMs} disabled={unavailable} onChange={(event) => setDurationMs(Number(event.target.value))} className="min-h-9 rounded-lg border border-zinc-700 bg-zinc-950 px-2 text-xs"><option value={1000}>1 秒</option><option value={2000}>2 秒</option><option value={5000}>5 秒</option><option value={10000}>10 秒</option></select>
          </label>
          <label className="flex items-center gap-2 text-xs text-zinc-300"><input type="checkbox" checked={reload} disabled={unavailable} onChange={(event) => setReload(event.target.checked)} />記録開始時にページを読み直す</label>
          <button type="button" disabled={unavailable} onClick={() => void onAction("browser.network.capture", { profile_id: profileId, tab_id: tabId, duration_ms: durationMs, reload, max_entries: 200 })} className="min-h-9 rounded-lg bg-zinc-100 px-4 text-xs font-semibold text-zinc-950 hover:bg-white disabled:opacity-40">記録を開始</button>
        </div>
        {capture && (
          <>
            <p role="status" className="text-xs text-zinc-400">{requests.length} 件の通信 · {consoleEntries.length} 件の Console イベント{capture.truncated === true ? " · 表示上限に達しました" : ""}</p>
            <div className="max-h-80 overflow-auto rounded-lg border border-zinc-800">
              <table className="w-full text-left text-xs">
                <caption className="sr-only">ネットワーク通信の記録</caption>
                <thead className="bg-zinc-900 text-zinc-400"><tr><th scope="col" className="p-2">状態</th><th scope="col" className="p-2">メソッド</th><th scope="col" className="p-2">URL</th><th scope="col" className="p-2">種類</th></tr></thead>
                <tbody>{requests.map((request, index) => <tr key={`${browserText(request.request_id)}:${index}`} className="border-t border-zinc-800 text-zinc-300"><td className="p-2">{browserText(request.status, request.error ? "Error" : "—")}</td><td className="p-2 font-mono">{browserText(request.method)}</td><td className="max-w-lg break-all p-2 font-mono">{browserText(request.url)}</td><td className="p-2">{browserText(request.type ?? request.mime_type)}</td></tr>)}</tbody>
              </table>
              {requests.length === 0 && <p className="p-3 text-xs text-zinc-500">記録中に通信はありませんでした。</p>}
            </div>
            <details className="text-xs text-zinc-400"><summary className="cursor-pointer py-2">Console イベント（{consoleEntries.length} 件）</summary><ul className="max-h-48 space-y-1 overflow-auto">{consoleEntries.map((entry, index) => <li key={index} className="break-all rounded-md bg-black/30 p-2 font-mono">{browserText(entry.level ?? entry.type, "log")} · {browserText(entry.url, "page")} {entry.line !== undefined ? `:${browserText(entry.line)}` : ""} · {browserText(entry.argument_count, "0")} arguments</li>)}</ul></details>
          </>
        )}
      </div>
    </section>
  );
}
