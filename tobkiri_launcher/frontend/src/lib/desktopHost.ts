import type {
  ApiPresentationSelection, ApiPresentationState, BackgroundControlStatus,
  DebugApprovalDuration, DebugApprovalStatus, DesktopSystemInfo,
  PresentationLaunchResponse,
} from './apiTypes';

type TauriInvoke = <T>(command: string, args?: Record<string, unknown>) => Promise<T>;
type TauriWindow = Window & {
  __TAURI_INTERNALS__?: unknown;
  __TAURI__?: {
    core?: {
      invoke?: TauriInvoke;
    };
  };
};

function getTauriInvoke(): TauriInvoke | null {
  const maybeWindow = window as TauriWindow;
  const invoke = maybeWindow.__TAURI__?.core?.invoke;
  return typeof invoke === 'function' ? invoke : null;
}

function isLikelyTauriShell(): boolean {
  const maybeWindow = window as TauriWindow;
  return Boolean(maybeWindow.__TAURI__ || maybeWindow.__TAURI_INTERNALS__);
}

export async function loadTauriInvoke(): Promise<TauriInvoke | null> {
  const globalInvoke = getTauriInvoke();
  if (globalInvoke) return globalInvoke;
  if (!isLikelyTauriShell()) return null;
  const mod = await import('@tauri-apps/api/core');
  return mod.invoke as TauriInvoke;
}

async function requireTauriInvoke(operation: string): Promise<TauriInvoke> {
  const invoke = await loadTauriInvoke();
  if (!invoke) {
    throw new Error(`${operation} is only available in Tobkiri Launcher.`);
  }
  return invoke;
}

export function isDesktopShellAvailable(): boolean {
  return getTauriInvoke() !== null || isLikelyTauriShell();
}

export async function openExternalUrl(url: string): Promise<void> {
  const invoke = await loadTauriInvoke();
  if (invoke) {
    await invoke('open_external_url', {url});
    return;
  }
  const destination = new URL(url, window.location.href);
  if (destination.protocol !== 'http:' && destination.protocol !== 'https:') {
    throw new Error('External navigation requires an HTTP(S) destination.');
  }
  const opened = window.open(destination.href, '_blank', 'noopener,noreferrer');
  if (!opened) throw new Error('The external destination could not be opened.');
}

export async function sendToBackground(): Promise<void> {
  const invoke = await requireTauriInvoke('Background control');
  await invoke<void>('send_to_background');
}

export async function showAppWindow(): Promise<void> {
  const invoke = await requireTauriInvoke('Window restore');
  await invoke<void>('show_app_window');
}

export async function fetchBackgroundControlStatus(): Promise<BackgroundControlStatus | null> {
  const invoke = await loadTauriInvoke();
  return invoke ? invoke<BackgroundControlStatus>('get_background_control_status') : null;
}

export async function fetchDesktopSystemInfo(): Promise<DesktopSystemInfo | null> {
  const invoke = await loadTauriInvoke();
  return invoke ? invoke<DesktopSystemInfo>('get_desktop_system_info') : null;
}

export async function fetchDebugApprovalStatus(): Promise<DebugApprovalStatus | null> {
  const invoke = await loadTauriInvoke();
  return invoke ? invoke<DebugApprovalStatus>('debug_approval_status') : null;
}

export async function armDebugApproval(duration: DebugApprovalDuration): Promise<DebugApprovalStatus> {
  const invoke = await requireTauriInvoke('Developer Debug Approval');
  return invoke<DebugApprovalStatus>('arm_debug_approval', {duration});
}

export async function revokeDebugApproval(): Promise<DebugApprovalStatus> {
  const invoke = await requireTauriInvoke('Developer Debug Approval');
  return invoke<DebugApprovalStatus>('revoke_debug_approval');
}

export async function launchDefaultspackDesktop(): Promise<string> {
  const invoke = await requireTauriInvoke('Defaultspack desktop launch');
  return invoke<string>('launch_defaultspack_desktop');
}

export async function fetchPresentationState(): Promise<ApiPresentationState> {
  const invoke = await requireTauriInvoke('Presentation selection');
  return invoke<ApiPresentationState>('get_presentation_catalog');
}

export async function selectPresentation(
  selection: ApiPresentationSelection,
): Promise<ApiPresentationState> {
  const invoke = await requireTauriInvoke('Presentation selection');
  return invoke<ApiPresentationState>('select_presentation', {selection});
}

export async function launchSelectedPresentation(): Promise<PresentationLaunchResponse> {
  const invoke = await requireTauriInvoke('Presentation launch');
  return invoke<PresentationLaunchResponse>('launch_selected_presentation');
}
