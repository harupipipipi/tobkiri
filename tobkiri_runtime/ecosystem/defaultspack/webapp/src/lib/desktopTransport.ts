export type TauriInvoke = <T = unknown>(command: string, args?: Record<string, unknown>) => Promise<T>;

type TauriWindow = Window & {
  __TAURI_INTERNALS__?: unknown;
  __TAURI__?: { core?: { invoke?: TauriInvoke } };
};

/** Only a browser without Tauri markers may use a non-native fallback. */
export async function loadTauriInvoke(
  loadCore: () => Promise<{ invoke: TauriInvoke }> = () => import("@tauri-apps/api/core"),
): Promise<TauriInvoke | null> {
  const nativeWindow = window as TauriWindow;
  const invoke = nativeWindow.__TAURI__?.core?.invoke;
  if (typeof invoke === "function") return invoke;
  if (!nativeWindow.__TAURI__ && !nativeWindow.__TAURI_INTERNALS__) return null;
  const core = await loadCore();
  if (typeof core.invoke !== "function") {
    throw new Error("Tobkiri Launcherへの接続を読み込めませんでした。");
  }
  return core.invoke;
}
