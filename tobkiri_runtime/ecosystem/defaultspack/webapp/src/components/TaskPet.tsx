import { useEffect, useRef, useState } from "react";
import { Bell, BellOff, CheckCircle2, Eye, EyeOff, LoaderCircle, PauseCircle, TriangleAlert, XCircle } from "lucide-react";
import type { SavedTurnEventSnapshot } from "../lib/api";
import { cn } from "../lib/cn";
import {
  TaskPetCompletionObserver, loadTaskPetPreference, saveTaskPetPreference,
  shouldSendDesktopNotification, taskPetBoundViewModel, type TaskPetScope,
} from "../lib/taskPet";
import { LayerPortal } from "../ui/layers/LayerPortal";

export type TaskPetProps = {
  profileId: string;
  scope: TaskPetScope | null;
  snapshot: SavedTurnEventSnapshot | null;
  snapshotProfileId: string | null;
  taskText?: string | null;
  activityText?: string | null;
  hidden?: boolean;
  raised?: boolean;
};
const IMAGE_SRC = "/static/pet/tobkiri-pet.png";

function permission(): NotificationPermission | "unsupported" {
  return typeof window !== "undefined" && "Notification" in window ? window.Notification.permission : "unsupported";
}
function storage(): Storage | null {
  try { return typeof window === "undefined" ? null : window.localStorage; } catch { return null; }
}

/** A passive display; completion comes only from the versioned turn snapshot. */
export function TaskPet(props: TaskPetProps) {
  // Remount Profile-local state so asynchronous permission replies cannot enable another Profile.
  return <ProfileTaskPet key={props.profileId} {...props} />;
}

function ProfileTaskPet({ profileId, scope, snapshot, snapshotProfileId, taskText, activityText, hidden = false, raised = false }: TaskPetProps) {
  const [enabled, setEnabled] = useState(() => loadTaskPetPreference(storage(), profileId, "enabled"));
  const [notifications, setNotifications] = useState(() => loadTaskPetPreference(storage(), profileId, "notifications"));
  const [notificationPermission, setNotificationPermission] = useState(permission);
  const [preferenceError, setPreferenceError] = useState<string | null>(null);
  const [pageVisible, setPageVisible] = useState(() => typeof document === "undefined" || document.visibilityState !== "hidden");
  const mounted = useRef(true);
  const observer = useRef(new TaskPetCompletionObserver());
  const view = taskPetBoundViewModel(profileId, scope, snapshotProfileId, snapshot, taskText, activityText);

  useEffect(() => {
    const update = () => setPageVisible(document.visibilityState !== "hidden");
    document.addEventListener("visibilitychange", update);
    return () => document.removeEventListener("visibilitychange", update);
  }, []);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  useEffect(() => {
    const outcome = observer.current.observe(view);
    const currentPermission = permission();
    if (!outcome || !enabled || currentPermission === "unsupported"
      || !shouldSendDesktopNotification(currentPermission, document.visibilityState, notifications, hidden)) return;
    try {
      const notice = new window.Notification(outcome === "completed" ? "Tobkiri: タスクが完了しました" : "Tobkiri: タスクを確認してください", {
        body: outcome === "completed" ? "回答が届きました。" : "会話画面で状態を確認してください。",
        icon: IMAGE_SRC,
        tag: `tobkiri-task-${view.key ?? ""}`,
      });
      notice.onclick = () => { window.focus(); notice.close(); };
    } catch { /* Unsupported/denied notifications must never interrupt conversation UI. */ }
  }, [enabled, hidden, notifications, scope?.turnId, view.key, view.revision, view.mood]);

  const save = (name: "enabled" | "notifications", value: boolean) => {
    if (name === "notifications" && !value) setNotifications(false);
    if (!saveTaskPetPreference(storage(), profileId, name, value)) {
      setPreferenceError("設定を保存できませんでした。保存領域を確認してください。");
      return false;
    }
    setPreferenceError(null);
    if (name === "enabled") setEnabled(value); else setNotifications(value);
    return true;
  };
  const enableNotifications = async () => {
    if (permission() === "unsupported") return;
    try {
      const result = await window.Notification.requestPermission();
      if (!mounted.current) return;
      setNotificationPermission(result);
      if (result === "granted") save("notifications", true);
    } catch { if (mounted.current) setNotificationPermission(permission()); }
  };
  if (hidden || !pageVisible) return null;
  if (!enabled) return <LayerPortal layer="panel"><aside className={cn("fixed right-3 sm:right-5", raised ? "bottom-20" : "bottom-4")}><button type="button" onClick={() => save("enabled", true)} className="rounded-full border border-zinc-700 bg-zinc-950 px-3 py-2 text-xs text-zinc-200"><Eye size={13} className="mr-1 inline" />Tobkiri ペットを表示</button>{preferenceError && <p role="alert">{preferenceError}</p>}</aside></LayerPortal>;
  const Icon = view.mood === "thinking" ? LoaderCircle : view.mood === "waiting" ? PauseCircle : view.mood === "completed" ? CheckCircle2 : view.mood === "error" ? TriangleAlert : view.mood === "cancelled" ? XCircle : Eye;
  return <LayerPortal layer="panel">
    <aside className={cn("task-pet pointer-events-none fixed right-3 flex w-[min(19rem,calc(100vw-1.5rem))] flex-col items-end sm:right-5", raised ? "bottom-20" : "bottom-4")} data-state={view.mood} aria-label="Tobkiri ペット">
      <img className="task-pet-character h-24 w-24 object-contain" src={IMAGE_SRC} alt="" aria-hidden="true" />
      <section className="task-pet-bubble pointer-events-auto w-full rounded-2xl border border-zinc-700 bg-zinc-950/95 px-4 py-3 shadow-xl" role="status" aria-live="polite">
        <div className="flex items-center gap-2 text-xs text-zinc-400"><Icon size={13} className={view.mood === "thinking" ? "task-pet-spinner" : ""} aria-hidden="true" />{view.label}</div>
        <p className="mt-1 text-sm font-semibold text-zinc-100">{view.title}</p><p className="mt-1 text-xs text-zinc-400">{view.detail}</p>
        <div className="mt-2 flex flex-wrap gap-2">
          {notificationPermission !== "unsupported" && <button type="button" onClick={() => notifications ? save("notifications", false) : void enableNotifications()} className="rounded-lg border border-zinc-700 px-2 py-1 text-xs text-zinc-300">{notifications ? <BellOff size={12} className="mr-1 inline" /> : <Bell size={12} className="mr-1 inline" />}{notifications ? "通知をOFF" : "バックグラウンド通知をON"}</button>}
          <button type="button" onClick={() => save("enabled", false)} className="rounded-lg border border-zinc-700 px-2 py-1 text-xs text-zinc-300"><EyeOff size={12} className="mr-1 inline" />非表示</button>
        </div>
        {notificationPermission === "denied" && <p className="mt-2 text-xs text-zinc-400">通知が拒否されています。ブラウザ設定で確認してください。</p>}
        {preferenceError && <p className="mt-2 text-xs text-amber-200" role="alert">{preferenceError}</p>}
      </section>
    </aside>
  </LayerPortal>;
}
