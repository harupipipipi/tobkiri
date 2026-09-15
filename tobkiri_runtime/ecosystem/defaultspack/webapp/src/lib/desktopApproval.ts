import type { AuthorityApprovalContext } from "./api";
import { loadTauriInvoke } from "./desktopTransport";

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

export async function launchActivePresentationFromAuxiliary(): Promise<boolean> {
  const invoke = await loadTauriInvoke();
  if (!invoke) return false;
  await invoke("launch_active_presentation_from_auxiliary");
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
