import { useEffect, useRef, useState } from "react";
import { Cookie } from "lucide-react";

import { browserCookieDomains, type BrowserActionRunner } from "../../features/browser/browserModel";
import { ErrorNotice } from "../ErrorNotice";

export function BrowserCookiesPanel({ profileId, disabled, receipt, onAction }: {
  profileId: string;
  disabled: boolean;
  receipt: string;
  onAction: BrowserActionRunner;
}) {
  const loadSequence = useRef(0);
  const [content, setContent] = useState("");
  const [filename, setFilename] = useState("");
  const [format, setFormat] = useState<"json" | "netscape">("json");
  const [domains, setDomains] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (receipt.startsWith("browser.cookies.import:")) { setContent(""); setFilename(""); }
  }, [receipt]);

  const loadFile = async (file: File | undefined) => {
    const sequence = ++loadSequence.current;
    setContent("");
    setFilename("");
    setError(null);
    if (!file) return;
    if (file.size > 2 * 1024 * 1024) { setError("Cookie ファイルは 2 MB 以下にしてください。"); return; }
    setLoading(true);
    try {
      const text = new TextDecoder("utf-8", { fatal: true }).decode(await file.arrayBuffer());
      if (sequence !== loadSequence.current) return;
      setContent(text);
      setFilename(file.name);
      setFormat(file.name.toLowerCase().endsWith(".json") || text.trim().startsWith("[") || text.trim().startsWith("{") ? "json" : "netscape");
    } catch { if (sequence === loadSequence.current) setError("UTF-8 の Cookie ファイルを選択してください。"); }
    finally { if (sequence === loadSequence.current) setLoading(false); }
  };

  const importCookies = async () => {
    setError(null);
    try {
      const selectedDomains = browserCookieDomains(domains);
      await onAction("browser.cookies.import", {
        profile_id: profileId,
        content,
        format,
        ...(selectedDomains.length ? { domains: selectedDomains } : {}),
      });
    } catch (err) { setError(err instanceof Error ? err.message : String(err)); }
  };

  return (
    <section aria-label="Import browser cookies" className="max-w-2xl space-y-4">
      <div>
        <h2 className="flex items-center gap-2 text-sm font-semibold text-zinc-100"><Cookie size={16} />他のブラウザから Cookie をインポート</h2>
        <p className="mt-1 text-xs leading-5 text-zinc-400">元のブラウザからエクスポートした JSON または Netscape 形式のファイルを選び、Tobkiri のブラウザでログイン状態を利用できます。</p>
      </div>
      <div className="grid gap-4 rounded-xl border border-zinc-800 bg-zinc-950/45 p-4">
        <label className="grid gap-1 text-xs text-zinc-300">Cookie ファイル
          <input type="file" accept=".json,.txt" aria-label="Cookie ファイル" disabled={disabled || loading} onChange={(event) => { void loadFile(event.target.files?.[0]); event.target.value = ""; }} className="min-w-0 text-xs text-zinc-400 file:mr-3 file:rounded-md file:border-0 file:bg-zinc-800 file:px-3 file:py-2 file:text-zinc-200" />
        </label>
        {filename && <p className="break-words text-xs text-zinc-400">{filename} · {Math.ceil(new TextEncoder().encode(content).length / 1024)} KB</p>}
        <label className="grid gap-1 text-xs text-zinc-300">形式
          <select value={format} aria-label="Cookie ファイル形式" disabled={disabled} onChange={(event) => setFormat(event.target.value as "json" | "netscape")} className="min-h-9 rounded-lg border border-zinc-700 bg-zinc-950 px-3 text-xs text-zinc-200"><option value="json">JSON</option><option value="netscape">Netscape cookies.txt</option></select>
        </label>
        <label className="grid gap-1 text-xs text-zinc-300">インポートするドメイン（省略すると全件）
          <input value={domains} aria-label="Cookie の対象ドメイン" disabled={disabled} onChange={(event) => setDomains(event.target.value)} placeholder="example.com, .example.org" className="min-h-9 rounded-lg border border-zinc-700 bg-zinc-950 px-3 text-xs text-zinc-200" />
        </label>
        <p className="text-xs leading-5 text-zinc-500">Cookie にはログイン情報が含まれます。選択中のプロファイルだけに保存し、ファイルの内容は画面に表示しません。</p>
        {loading && <p role="status" className="text-xs text-zinc-400">Cookie ファイルを読み込んでいます…</p>}
        {error && <ErrorNotice message={error} copyLabel="Cookie インポートのエラーをコピー" />}
        <button type="button" disabled={disabled || loading || !content.trim() || !profileId} onClick={() => void importCookies()} className="min-h-9 justify-self-start rounded-lg bg-zinc-100 px-4 text-xs font-semibold text-zinc-950 hover:bg-white disabled:opacity-40">確認してインポート</button>
      </div>
    </section>
  );
}
