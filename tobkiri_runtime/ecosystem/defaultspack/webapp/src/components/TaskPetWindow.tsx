import { useEffect, useRef, useState, type PointerEvent as ReactPointerEvent } from "react";
import { CheckCircle2, EyeOff, Grip, LoaderCircle, PauseCircle, TriangleAlert, XCircle } from "lucide-react";
import { isTaskPetPresentation, type TaskPetPresentation } from "../lib/taskPetWindow";
import { loadTauriInvoke } from "../lib/desktopTransport";
import { cn } from "../lib/cn";

const IMAGE_SRC = `${(import.meta as ImportMeta & { env?: { BASE_URL?: string } }).env?.BASE_URL || "/static/"}pet/tobkiri-pet.png`;

export function TaskPetWindow({ profileId }: { profileId: string }) {
  const [presentation, setPresentation] = useState<TaskPetPresentation>({
    profileId,
    enabled: true,
    view: { key: null, revision: 0, mood: "idle", label: "Tobkiri ペット", title: "ここで見守っています", detail: "タスクを始めると、進み具合をここに表示します。" },
  });
  const [error, setError] = useState<string | null>(null);
  const dragState = useRef<{
    mode: "pending" | "browser" | "native";
    startX: number;
    startY: number;
    startLeft: number;
    startTop: number;
    lastX: number;
    lastY: number;
    released: boolean;
    move: (event: PointerEvent) => void;
    up: () => void;
  } | null>(null);

  const finishDragListeners = () => {
    const state = dragState.current;
    if (!state) return;
    window.removeEventListener("pointermove", state.move);
    window.removeEventListener("pointerup", state.up);
    window.removeEventListener("pointercancel", state.up);
    dragState.current = null;
  };

  useEffect(() => {
    let disposed = false;
    let unlisten: (() => void) | undefined;
    let eventRevision = 0;
    const onBrowserMessage = (event: MessageEvent<unknown>) => {
      if (event.origin !== window.location.origin || event.source !== window.opener) return;
      const message = event.data as { type?: unknown; presentation?: unknown } | null;
      if (message?.type !== "task-pet-state" || !isTaskPetPresentation(message.presentation)
        || message.presentation.profileId !== profileId) return;
      eventRevision += 1;
      setPresentation(message.presentation);
    };
    const start = async () => {
      try {
        const invoke = await loadTauriInvoke();
        if (disposed) return;
        if (!invoke) {
          if (!window.opener) throw new Error("親画面からペットの状態を受け取れません。");
          window.addEventListener("message", onBrowserMessage);
          window.opener.postMessage({ type: "task-pet-ready" }, window.location.origin);
          return;
        }
        const { listen } = await import("@tauri-apps/api/event");
        const listener = await listen<TaskPetPresentation>("task-pet-state", ({ payload }) => {
          if (isTaskPetPresentation(payload) && payload.profileId === profileId) {
            eventRevision += 1; setPresentation(payload);
          }
        });
        if (disposed) { listener(); return; }
        unlisten = listener;
        const revisionBeforeFetch = eventRevision;
        const initial = await invoke<TaskPetPresentation>("task_pet_context");
        if (!isTaskPetPresentation(initial) || initial.profileId !== profileId) throw new Error("ペットの状態を確認できませんでした。");
        if (!disposed && eventRevision === revisionBeforeFetch) setPresentation(initial);
      } catch (cause) {
        if (!disposed) setError("ペットを接続できませんでした。Launcher の状態を確認してください。");
      }
    };
    void start();
    return () => { disposed = true; unlisten?.(); window.removeEventListener("message", onBrowserMessage); finishDragListeners(); };
  }, [profileId]);

  const hide = async () => {
    try {
      const invoke = await loadTauriInvoke();
      if (!invoke) {
        window.opener?.postMessage({ type: "task-pet-hidden", profileId }, window.location.origin);
        window.close();
        return;
      }
      await invoke("hide_task_pet");
    } catch (cause) {
      setError("ペットを閉じられませんでした。Launcher の状態を確認してください。");
    }
  };
  const drag = async (event: ReactPointerEvent<HTMLButtonElement>) => {
    if (event.button !== 0) return;
    finishDragListeners();
    const state = {
      mode: "pending" as "pending" | "browser" | "native",
      startX: event.screenX, startY: event.screenY,
      startLeft: window.screenX, startTop: window.screenY,
      lastX: event.screenX, lastY: event.screenY, released: false,
      move: (_moveEvent: PointerEvent) => {},
      up: () => {},
    };
    state.move = (moveEvent) => {
      state.lastX = moveEvent.screenX; state.lastY = moveEvent.screenY;
      if (state.mode === "browser") window.moveTo(state.startLeft + state.lastX - state.startX, state.startTop + state.lastY - state.startY);
    };
    state.up = () => { state.released = true; if (state.mode !== "pending") finishDragListeners(); };
    dragState.current = state;
    window.addEventListener("pointermove", state.move);
    window.addEventListener("pointerup", state.up);
    window.addEventListener("pointercancel", state.up);
    try {
      const invoke = await loadTauriInvoke();
      if (!invoke) {
        state.mode = "browser";
        window.moveTo(state.startLeft + state.lastX - state.startX, state.startTop + state.lastY - state.startY);
        if (state.released) finishDragListeners();
        return;
      }
      state.mode = "native";
      await invoke("drag_task_pet");
      if (state.released) finishDragListeners();
    } catch (cause) {
      finishDragListeners();
      setError("ペットを移動できませんでした。Launcher の状態を確認してください。");
    }
  };

  const view = presentation.view;
  const Icon = view.mood === "thinking" ? LoaderCircle : view.mood === "waiting" ? PauseCircle : view.mood === "completed" ? CheckCircle2 : view.mood === "error" ? TriangleAlert : view.mood === "cancelled" ? XCircle : Grip;
  return <main className="task-pet-window min-h-screen bg-transparent p-2" aria-label="Tobkiri ペット">
    <aside className="task-pet-window-card" data-state={view.mood}>
      <button type="button" className="task-pet-window-drag" onPointerDown={(event) => { void drag(event); }} aria-label="ペットをドラッグして移動">
        <img className="task-pet-character" src={IMAGE_SRC} alt="" aria-hidden="true" />
        <span className="task-pet-window-grip"><Grip size={14} /> 移動</span>
      </button>
      <section className="task-pet-bubble" role="status" aria-live="polite">
        <div className="task-pet-window-label"><Icon size={14} className={cn(view.mood === "thinking" && "task-pet-spinner")} aria-hidden="true" />{view.label}</div>
        <p className="task-pet-window-title">{view.title}</p><p className="task-pet-window-detail">{view.detail}</p>
        <button type="button" onClick={() => void hide()} className="task-pet-window-hide"><EyeOff size={13} />非表示</button>
        {error && <p className="task-pet-window-error" role="alert">{error}</p>}
      </section>
    </aside>
  </main>;
}
