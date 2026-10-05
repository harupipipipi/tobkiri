import { useEffect, useRef, useState } from "react";
import { Eye } from "lucide-react";
import type { SavedTurnEventSnapshot } from "../lib/api";
import {
  TaskPetCompletionObserver, loadTaskPetPreference, saveTaskPetPreference,
  shouldSendDesktopNotification, taskPetBoundViewModel, type TaskPetScope,
} from "../lib/taskPet";
import {
  isTaskPetWindowMessage, taskPetWindowName,
  taskPetWindowUrl, type TaskPetPresentation,
} from "../lib/taskPetWindow";
import { loadTauriInvoke } from "../lib/desktopTransport";
import { cn } from "../lib/cn";
import { LayerPortal } from "../ui/layers/LayerPortal";

export type TaskPetProps = {
  profileId: string;
  scope: TaskPetScope | null;
  snapshot: SavedTurnEventSnapshot | null;
  snapshotProfileId: string | null;
  taskText?: string | null;
  activityText?: string | null;
  hidden?: boolean;
};

const IMAGE_SRC = `${(import.meta as ImportMeta & { env?: { BASE_URL?: string } }).env?.BASE_URL || "/static/"}pet/tobkiri-pet.png`;

function storage(): Storage | null {
  try { return typeof window === "undefined" ? null : window.localStorage; } catch { return null; }
}

/** Publishes the validated task projection and keeps completion notifications in the main app. */
export function TaskPet(props: TaskPetProps) {
  return <ProfileTaskPet key={props.profileId} {...props} />;
}

function ProfileTaskPet({ profileId, scope, snapshot, snapshotProfileId, taskText, activityText, hidden = false }: TaskPetProps) {
  const [enabled, setEnabled] = useState(() => loadTaskPetPreference(storage(), profileId, "enabled"));
  const [notifications, setNotifications] = useState(() => loadTaskPetPreference(storage(), profileId, "notifications"));
  const [notificationPermission, setNotificationPermission] = useState<NotificationPermission | "unsupported">(() => typeof window !== "undefined" && "Notification" in window ? window.Notification.permission : "unsupported");
  const [notice, setNotice] = useState<string | null>(null);
  const popup = useRef<Window | null>(null);
  const hadBrowserPopup = useRef(false);
  const observer = useRef(new TaskPetCompletionObserver());
  const view = taskPetBoundViewModel(profileId, scope, snapshotProfileId, snapshot, taskText, activityText);
  const presentation: TaskPetPresentation = { profileId, view, enabled };
  const presentationRef = useRef(presentation);
  presentationRef.current = presentation;
  const syncQueue = useRef<Promise<void>>(Promise.resolve());
  const syncRevision = useRef(0);
  const wasEnabled = useRef(false);
  const listenerReady = useRef<Promise<void>>(Promise.resolve());
  const mounted = useRef(true);

  const sendToPopup = (target: Window, value: TaskPetPresentation) => {
    try { target.postMessage({ type: "task-pet-state", presentation: value }, window.location.origin); } catch { /* Closed popup. */ }
  };

  useEffect(() => {
    mounted.current = true;
    let disposed = false;
    let unlisten: (() => void) | undefined;
    const setup = async () => {
      try {
        const invoke = await loadTauriInvoke();
        if (disposed || !invoke) return;
        const { listen } = await import("@tauri-apps/api/event");
        const listener = await listen<{ profileId?: string }>("task-pet-hidden", ({ payload }) => {
          if (payload?.profileId !== profileId) return;
          setEnabled(false);
          saveTaskPetPreference(storage(), profileId, "enabled", false);
        });
        if (disposed) listener(); else unlisten = listener;
      } catch { /* Native window integration errors are shown on explicit open. */ }
    };
    listenerReady.current = setup();
    const onMessage = (event: MessageEvent<unknown>) => {
      const target = popup.current;
      if (!target || event.origin !== window.location.origin || event.source !== target
        || !isTaskPetWindowMessage(event.data)) return;
      if (event.data.type === "task-pet-ready") sendToPopup(target, presentationRef.current);
      if (event.data.type === "task-pet-hidden" && event.data.profileId === profileId) {
        target.close(); popup.current = null; hadBrowserPopup.current = false;
        save("enabled", false);
      }
    };
    window.addEventListener("message", onMessage);
    const timer = window.setInterval(() => {
      if (popup.current?.closed) { popup.current = null; }
      if (hadBrowserPopup.current && !popup.current) {
        // Manual browser closure is reflected in the launcher preference.
        if (presentationRef.current.enabled) save("enabled", false);
        hadBrowserPopup.current = false;
      }
    }, 500);
    return () => {
      disposed = true;
      mounted.current = false;
      unlisten?.();
      window.removeEventListener("message", onMessage);
      window.clearInterval(timer);
      popup.current?.close(); popup.current = null;
    };
  // The listener handles popup handshake; updates are sent in the effect below.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [profileId]);

  useEffect(() => {
    let disposed = false;
    const revision = ++syncRevision.current;
    const update = async () => {
      try {
        await listenerReady.current;
        const invoke = await loadTauriInvoke();
        if (disposed || revision !== syncRevision.current) return;
        if (invoke) {
          const current = { ...presentationRef.current };
          const initialOrEnable = current.enabled && !wasEnabled.current;
          await invoke("sync_task_pet", { presentation: current, open: initialOrEnable });
          if (revision === syncRevision.current) wasEnabled.current = enabled;
        }
        const target = popup.current;
        if (target && !target.closed) sendToPopup(target, presentationRef.current);
      } catch { /* Passive updates should not surface repeated transport errors. */ }
    };
    syncQueue.current = syncQueue.current.catch(() => undefined).then(update);
    return () => { disposed = true; };
  }, [enabled, profileId, view.key, view.revision, view.mood, view.label, view.title, view.detail]);

  useEffect(() => {
    const outcome = observer.current.observe(view);
    if (!outcome || !enabled || hidden || !notifications || !("Notification" in window)
      || !shouldSendDesktopNotification(window.Notification.permission, document.visibilityState, notifications, hidden)) return;
    try {
      const notice = new window.Notification(outcome === "completed" ? "Tobkiri: タスクが完了しました" : "Tobkiri: タスクを確認してください", {
        body: outcome === "completed" ? "回答が届きました。" : "会話画面で状態を確認してください。",
        icon: IMAGE_SRC,
        tag: `tobkiri-task-${view.key ?? ""}`,
      });
      notice.onclick = () => { window.focus(); notice.close(); };
    } catch { /* Notifications are a passive convenience. */ }
  }, [enabled, hidden, notifications, view.key, view.revision, view.mood]);

  const save = (name: "enabled" | "notifications", value: boolean) => {
    if (!saveTaskPetPreference(storage(), profileId, name, value)) {
      setNotice("設定を保存できませんでした。"); return false;
    }
    if (name === "enabled") setEnabled(value); else setNotifications(value);
    setNotice(null);
    return true;
  };
  const open = async () => {
    if (!save("enabled", true)) return;
    // This user request owns reopening; the enabling effect only sends state.
    wasEnabled.current = true;
    try {
      const invoke = await loadTauriInvoke();
      if (invoke) {
        await listenerReady.current;
        syncQueue.current = syncQueue.current.catch(() => undefined).then(async () => {
          if (!mounted.current) return;
          await invoke("sync_task_pet", { presentation: { ...presentationRef.current, enabled: true }, open: true });
          wasEnabled.current = true;
        });
        await syncQueue.current;
      } else {
        const target = popup.current;
        if (target && !target.closed) { target.focus(); sendToPopup(target, { ...presentation, enabled: true }); return; }
        const opened = window.open(taskPetWindowUrl(window.location.href, profileId), taskPetWindowName(profileId), "popup=yes,width=336,height=370,resizable=yes");
        if (!opened) { setNotice("ポップアップがブロックされました。ブラウザーの設定で許可してください。"); return; }
        popup.current = opened;
        hadBrowserPopup.current = true;
        sendToPopup(opened, { ...presentation, enabled: true });
      }
    } catch (cause) {
      setNotice("ペットウィンドウを開けませんでした。Launcher の状態を確認してください。");
    }
  };
  const enableNotifications = async () => {
    if (notificationPermission === "unsupported") return;
    try {
      const permission = await window.Notification.requestPermission();
      if (!mounted.current) return;
      setNotificationPermission(permission);
      if (permission === "granted") save("notifications", true);
    } catch { if (mounted.current) setNotificationPermission(window.Notification.permission); }
  };

  return <LayerPortal layer="panel"><div className="task-pet-launcher">
    <button type="button" onClick={() => void open()} className="task-pet-launcher-button"><Eye size={14} />{enabled ? "ペットを開く" : "ペットを表示"}</button>
    {notificationPermission !== "unsupported" && <button type="button" onClick={() => notifications ? save("notifications", false) : void enableNotifications()} className="task-pet-launcher-notifications">{notifications ? "通知 ON" : "通知 OFF"}</button>}
    {notificationPermission === "denied" && <p className="task-pet-launcher-notice" role="status">通知が拒否されています。ブラウザー設定で確認してください。</p>}
    {notice && <p className={cn("task-pet-launcher-notice")} role="alert">{notice}</p>}
  </div></LayerPortal>;
}
