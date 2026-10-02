import { useEffect, useRef, useState } from "react";
import { FolderOpen, PackagePlus, Trash2 } from "lucide-react";

import { browserRows, browserText, readBrowserExtensionFiles, type BrowserActionRunner, type BrowserExtensionDraft, type BrowserResult } from "../../features/browser/browserModel";
import { ErrorNotice } from "../ErrorNotice";

export function BrowserExtensionsPanel({ extensions, profileId, disabled, receipt, onAction }: {
  extensions: BrowserResult[];
  profileId: string;
  disabled: boolean;
  receipt: string;
  onAction: BrowserActionRunner;
}) {
  const folderInput = useRef<HTMLInputElement>(null);
  const loadSequence = useRef(0);
  const [draft, setDraft] = useState<BrowserExtensionDraft | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => { folderInput.current?.setAttribute("webkitdirectory", ""); }, []);
  useEffect(() => {
    if (receipt.startsWith("browser.extensions.install:")) setDraft(null);
  }, [receipt]);

  const loadFiles = async (files: File[]) => {
    const sequence = ++loadSequence.current;
    setLoading(true);
    setError(null);
    setDraft(null);
    try {
      const next = await readBrowserExtensionFiles(files);
      if (sequence === loadSequence.current) setDraft(next);
    } catch (err) {
      if (sequence === loadSequence.current) setError(err instanceof Error ? err.message : String(err));
    } finally {
      if (sequence === loadSequence.current) setLoading(false);
    }
  };

  return (
    <section aria-label="Browser extensions" className="space-y-4">
      <div>
        <h2 className="flex items-center gap-2 text-sm font-semibold text-zinc-100"><PackagePlus size={16} />拡張機能</h2>
        <p className="mt-1 text-xs leading-5 text-zinc-400">AI が作った拡張機能をフォルダごと追加できます。Manifest V3 のテキストファイルに対応します。</p>
      </div>
      <div className="grid gap-3 rounded-xl border border-zinc-800 bg-zinc-950/45 p-4">
        <label className="grid gap-1 text-xs text-zinc-300">
          <span className="flex items-center gap-1.5"><FolderOpen size={14} />拡張機能のフォルダ</span>
          <input ref={folderInput} type="file" multiple disabled={disabled || loading} aria-label="拡張機能のフォルダ" onChange={(event) => { void loadFiles(Array.from(event.target.files ?? [])); event.target.value = ""; }} className="min-w-0 text-xs text-zinc-400 file:mr-3 file:rounded-md file:border-0 file:bg-zinc-800 file:px-3 file:py-2 file:text-zinc-200" />
        </label>
        <label className="grid gap-1 text-xs text-zinc-400">
          <span>または manifest.json と関連ファイルを選択</span>
          <input type="file" multiple disabled={disabled || loading} aria-label="拡張機能のファイル" onChange={(event) => { void loadFiles(Array.from(event.target.files ?? [])); event.target.value = ""; }} className="min-w-0 text-xs file:mr-3 file:rounded-md file:border-0 file:bg-zinc-800 file:px-3 file:py-2 file:text-zinc-200" />
        </label>
        {loading && <p role="status" className="text-xs text-zinc-400">拡張機能を読み込んでいます…</p>}
        {error && <ErrorNotice message={error} copyLabel="拡張機能のエラーをコピー" />}
        {draft && (
          <div className="space-y-3 rounded-lg border border-zinc-700 bg-black/25 p-3">
            <p className="text-sm font-semibold text-zinc-100">{draft.name} <span className="text-xs font-normal text-zinc-500">v{draft.version}</span></p>
            <dl className="grid gap-2 text-xs">
              <div><dt className="text-zinc-500">アクセス権限</dt><dd className="mt-1 break-words font-mono text-zinc-300">{draft.permissions.join(", ") || "指定なし"}</dd></div>
              <div><dt className="text-zinc-500">アクセスするサイト</dt><dd className="mt-1 break-words font-mono text-zinc-300">{draft.hostPermissions.join(", ") || "指定なし"}</dd></div>
            </dl>
            <p className="text-xs text-zinc-500">Content Scripts と任意のアクセス権限も含みます。</p>
            <p className="text-xs text-zinc-500">{Object.keys(draft.files).length} ファイル · 選択中のプロファイルに追加します。</p>
            <button type="button" disabled={disabled || !profileId} onClick={() => void onAction("browser.extensions.install", { profile_id: profileId, files: draft.files })} className="min-h-9 rounded-lg bg-zinc-100 px-4 text-xs font-semibold text-zinc-950 hover:bg-white disabled:opacity-40">権限を確認して追加</button>
          </div>
        )}
      </div>
      <p className="text-xs leading-5 text-zinc-500">追加・削除は起動中のブラウザに反映され、保存した拡張機能は次回起動時にも読み込まれます。</p>
      <ul className="space-y-2">
        {extensions.map((extension) => (
          <li key={browserText(extension.extension_id)} className="flex items-start gap-3 rounded-lg border border-zinc-800 p-3">
            <div className="min-w-0 flex-1">
              <p className="text-sm text-zinc-200">{browserText(extension.name, "Extension")} <span className="text-xs text-zinc-500">{browserText(extension.version)}</span></p>
              <p className="mt-1 break-words text-xs text-zinc-500">{Array.isArray(extension.permissions) ? extension.permissions.map((permission) => browserText(permission)).join(", ") : ""}</p>
              <p className="mt-1 text-xs text-zinc-500">{extension.loaded === true ? "起動時に読み込み済み" : "保存済み"}</p>
            </div>
            <button type="button" disabled={disabled || !profileId} aria-label={`${browserText(extension.name, "Extension")} を削除`} onClick={() => void onAction("browser.extensions.remove", { profile_id: profileId, extension_id: extension.extension_id })} className="flex min-h-9 min-w-9 items-center justify-center rounded-md text-zinc-400 hover:bg-red-500/10 hover:text-red-200 disabled:opacity-40"><Trash2 size={15} /></button>
          </li>
        ))}
      </ul>
      {browserRows(extensions).length === 0 && <p className="text-xs text-zinc-500">このプロファイルには拡張機能がありません。</p>}
    </section>
  );
}
