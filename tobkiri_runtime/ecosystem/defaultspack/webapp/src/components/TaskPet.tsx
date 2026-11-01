import { useEffect, useImperativeHandle, useRef, useState, type Ref } from "react";
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
import { taskPetStorage, useTaskPetNotifications } from "../lib/taskPetPreferences";

export type TaskPetHandle = { profileId: string; open: () => Promise<boolean> };

export type TaskPetProps = {
  profileId: string;
  scope: TaskPetScope | null;
  snapshot: SavedTurnEventSnapshot | null;
  snapshotProfileId: string | null;
  taskText?: string | null;
  activityText?: string | null;
  hidden?: boolean;
  controllerRef?: Ref<TaskPetHandle>;
  onError?: (message: string) => void;
};

const IMAGE_SRC = `${(import.meta as ImportMeta & { env?: { BASE_URL?: string } }).env?.BASE_URL || "/static/"}pet/tobkiri-pet.png`;

/** Publishes the validated task projection and keeps completion notifications in the main app. */
export function TaskPet(props: TaskPetProps) {
  return <ProfileTaskPet key={props.profileId} {...props} />;
}

function ProfileTaskPet({ profileId, scope, snapshot, snapshotProfileId, taskText, activityText, hidden = false, controllerRef, onError }: TaskPetProps) {
  const [enabled, setEnabled] = useState(() => loadTaskPetPreference(taskPetStorage(), profileId, "enabled"));
  const notifications = useTaskPetNotifications(profileId);
  const onErrorRef = useRef(onError);
  onErrorRef.current = onError;
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

  const recordHidden = () => {
    setEnabled(false);
    if (!saveTaskPetPreference(taskPetStorage(), profileId, "enabled", false)) {
      onErrorRef.current?.("設定を保存できませんでした。");
    }
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
          recordHidden();
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
        recordHidden();
      }
    };
    window.addEventListener("message", onMessage);
    const timer = window.setInterval(() => {
      if (popup.current?.closed) { popup.current = null; }
      if (hadBrowserPopup.current && !popup.current) {
        // Manual browser closure is reflected in the launcher preference.
        if (presentationRef.current.enabled) recordHidden();
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

  const open = async (): Promise<boolean> => {
    if (!saveTaskPetPreference(taskPetStorage(), profileId, "enabled", true)) {
      onErrorRef.current?.("設定を保存できませんでした。");
      return false;
    }
    setEnabled(true);
    // This explicit request owns reopening; the enabling effect only sends state.
    wasEnabled.current = true;
    try {
      const invoke = await loadTauriInvoke();
      if (!mounted.current) return false;
      if (invoke) {
        await listenerReady.current;
        let opened = false;
        syncQueue.current = syncQueue.current.catch(() => undefined).then(async () => {
          if (!mounted.current) return;
          await invoke("sync_task_pet", { presentation: { ...presentationRef.current, enabled: true }, open: true });
          wasEnabled.current = true;
          opened = true;
        });
        await syncQueue.current;
        return opened;
      }
      const target = popup.current;
      if (target && !target.closed) { target.focus(); sendToPopup(target, { ...presentationRef.current, enabled: true }); return true; }
      const opened = window.open(taskPetWindowUrl(window.location.href, profileId), taskPetWindowName(profileId), "popup=yes,width=336,height=370,resizable=yes");
      if (!opened) { onErrorRef.current?.("ポップアップがブロックされました。ブラウザーの設定で許可してください。"); return false; }
      popup.current = opened;
      hadBrowserPopup.current = true;
      sendToPopup(opened, { ...presentationRef.current, enabled: true });
      return true;
    } catch {
      if (mounted.current) onErrorRef.current?.("ペットウィンドウを開けませんでした。Launcher の状態を確認してください。");
      return false;
    }
  };
  useImperativeHandle(controllerRef, () => ({ profileId, open }));
  return null;
}
