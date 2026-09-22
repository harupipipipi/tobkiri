import { useEffect, useRef, useState } from "react";
import {
  Bell,
  BellRing,
  CheckCircle2,
  Eye,
  EyeOff,
  LoaderCircle,
  TriangleAlert,
} from "lucide-react";

import { cn } from "../lib/cn";
import {
  shouldSendDesktopNotification,
  loadTaskPetEnabled,
  saveTaskPetEnabled,
  taskPetViewModel,
  type TaskPetMood,
} from "../lib/taskPet";
import { LayerPortal } from "../ui/layers/LayerPortal";

type TaskPetProps = {
  activityText?: string | null;
  completionKey?: string | null;
  error?: string | null;
  hidden?: boolean;
  isRunning: boolean;
  raised?: boolean;
  taskText?: string | null;
};

const COMPLETION_VISIBLE_MS = 12_000;
const TASK_PET_IMAGE_SRC = "/static/pet/tobkiri-pet.png";

function notificationPermission(): NotificationPermission | "unsupported" {
  if (typeof window === "undefined" || !("Notification" in window)) return "unsupported";
  return window.Notification.permission;
}

function preferenceStorage(): Storage | null {
  if (typeof window === "undefined") return null;
  try {
    return window.localStorage;
  } catch {
    return null;
  }
}

function sendCompletionNotification(
  mood: Extract<TaskPetMood, "completed" | "error">,
  completionKey: string | null | undefined,
) {
  const permission = notificationPermission();
  if (permission === "unsupported"
    || !shouldSendDesktopNotification(permission, document.visibilityState)) return;
  const notification = new window.Notification(
    mood === "completed" ? "Tobkiri: タスクが完了しました" : "Tobkiri: タスクを確認してください",
    {
      body: mood === "completed" ? "回答が届きました。" : "タスクが途中で止まりました。",
      icon: TASK_PET_IMAGE_SRC,
      tag: completionKey ? `tobkiri-task-${completionKey}` : "tobkiri-task-complete",
    },
  );
  notification.onclick = () => {
    window.focus();
    notification.close();
  };
}

export function TaskPet({
  activityText,
  completionKey,
  error,
  hidden = false,
  isRunning,
  raised = false,
  taskText,
}: TaskPetProps) {
  const [mood, setMood] = useState<TaskPetMood>(isRunning ? "thinking" : "idle");
  const [permission, setPermission] = useState<NotificationPermission | "unsupported">(
    notificationPermission,
  );
  const [isPetEnabled, setIsPetEnabled] = useState(() => loadTaskPetEnabled(preferenceStorage()));
  const [preferenceError, setPreferenceError] = useState<string | null>(null);
  const wasRunningRef = useRef(isRunning);

  useEffect(() => {
    const wasRunning = wasRunningRef.current;
    wasRunningRef.current = isRunning;
    if (isRunning) {
      setMood("thinking");
      return;
    }
    if (!wasRunning) return;

    const completedMood = error ? "error" : "completed";
    setMood(completedMood);
    sendCompletionNotification(completedMood, completionKey);
    const resetTimer = window.setTimeout(() => setMood("idle"), COMPLETION_VISIBLE_MS);
    return () => window.clearTimeout(resetTimer);
  }, [completionKey, error, isRunning]);

  const view = taskPetViewModel(mood, taskText, activityText, error);
  const enableNotifications = async () => {
    if (notificationPermission() === "unsupported") return;
    try {
      setPermission(await window.Notification.requestPermission());
    } catch {
      setPermission(notificationPermission());
    }
  };

  const setPetEnabled = (enabled: boolean) => {
    if (!saveTaskPetEnabled(preferenceStorage(), enabled)) {
      setPreferenceError("表示設定を保存できませんでした。ブラウザの保存領域を確認してください。");
      return;
    }
    setPreferenceError(null);
    setIsPetEnabled(enabled);
  };

  if (!isPetEnabled) {
    return (
      <LayerPortal layer="globalOverlay">
        <aside
          className={cn(
            "task-pet-restore fixed right-3 sm:right-5",
            raised ? "bottom-20" : "bottom-4",
            hidden && "hidden",
          )}
        >
          <button
            type="button"
            className="inline-flex items-center gap-2 rounded-full border border-zinc-700 bg-zinc-950/94 px-3 py-2 text-xs font-semibold text-zinc-200 shadow-xl backdrop-blur-xl transition hover:border-sky-300 hover:text-white"
            onClick={() => setPetEnabled(true)}
          >
            <Eye size={15} aria-hidden="true" />
            Tobkiri ペットを表示
          </button>
          {preferenceError && <p role="alert">{preferenceError}</p>}
        </aside>
      </LayerPortal>
    );
  }

  return (
    <LayerPortal layer="globalOverlay">
      <aside
        className={cn(
          "task-pet pointer-events-none fixed right-3 flex w-[min(19rem,calc(100vw-1.5rem))] flex-col items-end sm:right-5",
          raised ? "bottom-20" : "bottom-4",
          hidden && "hidden",
        )}
        data-state={view.mood}
        aria-label="Tobkiri ペット"
      >
        <img
          className="task-pet-character h-28 w-28 object-contain sm:h-32 sm:w-32"
          src={TASK_PET_IMAGE_SRC}
          alt=""
          aria-hidden="true"
        />
        <section
          className="task-pet-bubble pointer-events-auto w-full rounded-2xl border border-zinc-700/80 bg-zinc-950/94 px-4 py-3 shadow-2xl shadow-black/45 backdrop-blur-xl"
          role="status"
          aria-live={view.mood === "completed" || view.mood === "error" ? "assertive" : "polite"}
        >
          <div className="flex items-center gap-2 text-[11px] font-semibold tracking-[0.08em] text-zinc-400">
            {view.mood === "thinking" && <LoaderCircle size={13} className="task-pet-spinner text-sky-300" />}
            {view.mood === "completed" && <CheckCircle2 size={13} className="text-emerald-300" />}
            {view.mood === "error" && <TriangleAlert size={13} className="text-amber-300" />}
            {view.mood === "idle" && <span className="task-pet-idle-dot" aria-hidden="true" />}
            <span>{view.label}</span>
            {permission === "granted" && (
              <span className="ml-auto inline-flex items-center gap-1 text-[10px] font-medium normal-case tracking-normal text-emerald-300/80">
                <BellRing size={11} />
                通知ON
              </span>
            )}
          </div>
          <p className="mt-1.5 text-sm font-semibold leading-5 text-zinc-100">{view.title}</p>
          <p className="mt-1 text-xs leading-5 text-zinc-400">{view.detail}</p>
          {permission === "default" && (
            <button
              type="button"
              onClick={() => void enableNotifications()}
              className="mt-2 inline-flex h-7 items-center gap-1.5 rounded-lg border border-zinc-700 px-2.5 text-[11px] font-semibold text-zinc-300 transition hover:border-zinc-500 hover:bg-zinc-900 hover:text-white"
            >
              <Bell size={12} />
              バックグラウンド通知をON
            </button>
          )}
          {permission === "denied" && (
            <p className="mt-2 text-[10px] leading-4 text-zinc-600">
              ブラウザ設定で通知を許可すると、画面を離れていてもお知らせします。
            </p>
          )}
          <button
            type="button"
            onClick={() => setPetEnabled(false)}
            className="mt-2 inline-flex h-7 items-center gap-1.5 rounded-lg border border-zinc-700 px-2.5 text-[11px] font-semibold text-zinc-300 transition hover:border-zinc-500 hover:bg-zinc-900 hover:text-white"
          >
            <EyeOff size={12} />
            ペットを非表示
          </button>
          {preferenceError && (
            <p className="mt-2 text-[10px] leading-4 text-amber-200" role="alert">
              {preferenceError}
            </p>
          )}
        </section>
      </aside>
    </LayerPortal>
  );
}
