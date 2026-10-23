import { useState } from "react";
import { cn } from "../lib/cn";
import {
  saveToolSelectionControlPreference,
  toolSelectionControlPreferenceKey,
  useToolSelectionControlPreference,
} from "../lib/toolSelectionControlPreference";

/** Render the browser-local visibility control without a Host settings mutation. */
export function ToolSelectionControlSettings({ profileId }: { profileId?: string }) {
  const visible = useToolSelectionControlPreference(profileId);
  const [error, setError] = useState<string | null>(null);
  return <ToolSelectionControlSettingsView
    visible={visible}
    disabled={toolSelectionControlPreferenceKey(profileId) === null}
    error={error}
    onChange={(next) => setError(saveToolSelectionControlPreference(profileId, next)
      ? null : "表示設定をこのブラウザーに保存できませんでした。")}
  />;
}

/** Keep the display result and storage failure explicit for the user. */
export function ToolSelectionControlSettingsView({
  visible, disabled, error, onChange,
}: {
  visible: boolean;
  disabled: boolean;
  error: string | null;
  onChange: (visible: boolean) => void;
}) {
  return <div className="rounded-lg border border-zinc-800 bg-zinc-950/35 px-4 py-3">
    <button
      type="button"
      role="switch"
      aria-checked={visible}
      aria-label="入力欄に「機能の使い方」を表示"
      disabled={disabled}
      onClick={() => onChange(!visible)}
      className="flex w-full items-center justify-between gap-3 text-left disabled:opacity-50"
    >
      <span className="text-sm font-medium text-zinc-200">入力欄に「機能の使い方」を表示</span>
      <span aria-hidden="true" className={cn("rounded-md border px-2 py-1 text-xs", visible
        ? "border-sky-500/60 bg-sky-500/10 text-sky-200" : "border-zinc-700 text-zinc-400")}>{visible ? "ON" : "OFF"}</span>
    </button>
    <p className="mt-1 text-xs leading-5 text-zinc-500">普段は非表示です。このブラウザーの現在の Profile に保存します。表示すると、メッセージごとに自動・手動・ツールなしを選べます。</p>
    {disabled && <p role="status" className="mt-1 text-xs text-zinc-400">Profile を確認してから表示設定を変更できます。</p>}
    {error && <p role="alert" className="mt-1 text-xs text-red-300">{error}</p>}
  </div>;
}
