import type { AuthorityApprovalContext } from "./api";

type TauriInvoke = <T = unknown>(command: string, args?: Record<string, unknown>) => Promise<T>;

type TauriWindow = Window & {
  __TAURI_INTERNALS__?: unknown;
  __TAURI__?: {
    core?: {
      invoke?: TauriInvoke;
    };
  };
};

function tauriInvoke(): TauriInvoke | null {
  const invoke = (window as TauriWindow).__TAURI__?.core?.invoke;
  return typeof invoke === "function" ? invoke : null;
}

function isLikelyTauri(): boolean {
  const maybeWindow = window as TauriWindow;
  return Boolean(maybeWindow.__TAURI__ || maybeWindow.__TAURI_INTERNALS__);
}

async function loadTauriInvoke(): Promise<TauriInvoke | null> {
  const globalInvoke = tauriInvoke();
  if (globalInvoke) return globalInvoke;
  if (!isLikelyTauri()) return null;
  try {
    const mod = await import("@tauri-apps/api/core");
    return mod.invoke as TauriInvoke;
  } catch {
    return null;
  }
}

export async function openAuthorityApprovalWindow(requestId: string): Promise<boolean> {
  const invoke = await loadTauriInvoke();
  if (!invoke) return false;
  await invoke("open_authority_approval_window", { requestId });
  return true;
}

export async function openAmbientTriggerWindow(): Promise<boolean> {
  const invoke = await loadTauriInvoke();
  if (!invoke) return false;
  await invoke("open_ambient_trigger_window");
  return true;
}

export async function openFingerRecordingWindow(): Promise<boolean> {
  const invoke = await loadTauriInvoke();
  if (!invoke) return false;
  await invoke("open_finger_recording_window");
  return true;
}

export async function openDefaultspackMainWindow(path = "/chat"): Promise<boolean> {
  const invoke = await loadTauriInvoke();
  if (!invoke) return false;
  await invoke("open_defaultspack_main_window", { path });
  return true;
}

export async function openDefaultsConsoleWindow(): Promise<boolean> {
  const invoke = await loadTauriInvoke();
  if (!invoke) return false;
  await invoke("open_defaults_console_window");
  return true;
}

export async function openHostPermissionsPageWindow(): Promise<boolean> {
  const invoke = await loadTauriInvoke();
  if (!invoke) return false;
  await invoke("open_host_permissions_window");
  return true;
}

export async function openHostPermissionSettings(permissionId: string): Promise<boolean> {
  const invoke = await loadTauriInvoke();
  if (!invoke) return false;
  await invoke("open_host_permission_settings", { permissionId });
  return true;
}

export async function closeCurrentWindow(): Promise<boolean> {
  const invoke = await loadTauriInvoke();
  if (!invoke) return false;
  await invoke("close_current_window");
  return true;
}

export async function openHostPermissionsWindow(permissionId: string): Promise<boolean> {
  return openHostPermissionSettings(permissionId);
}

export type InteractiveApprovalOperatorBinding = {
  decision: "approve" | "deny";
  requestSnapshotDigest: string;
  typedConfirmationDigest: string | null;
};

export async function getAuthorityApprovalContext(
  requestId: string,
  binding?: InteractiveApprovalOperatorBinding,
): Promise<AuthorityApprovalContext> {
  const invoke = await loadTauriInvoke();
  if (!invoke) {
    throw new Error("承認コンテキストは Tobkiri Launcher の専用ウィンドウでのみ利用できます。");
  }
  return invoke<AuthorityApprovalContext>("authority_approval_context", {
    requestId,
    decision: binding?.decision ?? null,
    requestSnapshotDigest: binding?.requestSnapshotDigest ?? null,
    typedConfirmationDigest: binding?.typedConfirmationDigest ?? null,
  });
}
