import { useEffect, useRef, useState } from "react";
import { normalizeLocale, type LocaleSetting } from "../lib/i18n";
import { enableTaskPetNotifications, saveTaskPetNotifications, useTaskPetNotifications } from "../lib/taskPetPreferences";

/** Explicit opt-in for task completion and failure notifications from the pet. */
export function TaskPetNotificationSettings({ profileId, locale }: { profileId: string; locale?: LocaleSetting }) {
  return <ProfileNotificationSettings key={profileId} profileId={profileId} locale={locale} />;
}

function ProfileNotificationSettings({ profileId, locale }: { profileId: string; locale?: LocaleSetting }) {
  const enabled = useTaskPetNotifications(profileId);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const mounted = useRef(true);
  const operation = useRef(0);
  const ja = normalizeLocale(locale ?? "auto") === "ja";
  const supported = typeof window !== "undefined" && "Notification" in window;
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; operation.current++; };
  }, []);
  const toggle = async () => {
    const revision = ++operation.current;
    setError(null);
    if (enabled) {
      if (!saveTaskPetNotifications(profileId, false)) setError(ja ? "設定を保存できませんでした。" : "Could not save this setting.");
      return;
    }
    if (!supported) return;
    setPending(true);
    const result = await enableTaskPetNotifications({ profileId, requestPermission: () => window.Notification.requestPermission(), isCurrent: () => mounted.current && operation.current === revision });
    if (!mounted.current || operation.current !== revision) return;
    setPending(false);
    if (result === "storage-error") setError(ja ? "設定を保存できませんでした。" : "Could not save this setting.");
    if (result === "denied" || result === "default") setError(ja ? "通知が許可されていません。ブラウザー設定で確認してください。" : "Notifications are not allowed. Check your browser settings.");
    if (result === "request-error") setError(ja ? "通知の許可を確認できませんでした。" : "Could not request notification permission.");
  };
  return <section className="space-y-3 rounded-xl border border-white/[0.08] bg-white/[0.025] p-4" aria-label="Tobkiri pet">
    <h4 className="text-sm font-medium text-zinc-100">{ja ? "Tobkiri ペット" : "Tobkiri pet"}</h4>
    <p className="text-xs text-zinc-400">{ja ? "入力欄で /pet を実行すると、別ウィンドウのペットを表示できます。" : "Run /pet in the composer to open the pet in its own window."}</p>
    <label className="flex items-center justify-between gap-3">
      <span>{ja ? "ペットのタスク完了通知" : "Pet task completion notifications"}</span>
      <button type="button" role="switch" aria-checked={enabled} aria-label={ja ? "ペットのタスク完了通知" : "Pet task completion notifications"} disabled={pending || (!supported && !enabled)} onClick={() => void toggle()} className="min-w-14 rounded-md border border-zinc-700 px-3 py-1.5 text-xs font-medium text-zinc-200 hover:bg-white/[0.06] focus-visible:outline focus-visible:outline-2 focus-visible:outline-indigo-400 disabled:cursor-not-allowed disabled:opacity-50">{enabled ? "ON" : "OFF"}</button>
    </label>
    <p className="text-xs text-zinc-400">{ja ? "タスク完了・失敗時に、会話画面が非表示なら通知します。エラーバナーの通知とは別の設定です。" : "Notify when a task completes or fails while the conversation is hidden. This is separate from error-banner notifications."}</p>
    {!supported && <p role="status">{ja ? "この環境はデスクトップ通知に対応していません。" : "Desktop notifications are unavailable in this environment."}</p>}
    {supported && window.Notification.permission === "denied" && <p role="status">{ja ? "通知が拒否されています。ブラウザー設定で確認してください。" : "Notification permission is denied. Check your browser settings."}</p>}
    {error && <p role="alert">{error}</p>}
  </section>;
}
