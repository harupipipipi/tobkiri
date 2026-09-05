import { defaultspackApiFetch, defaultspackContractRoute } from "./api";

export type DesktopPermissionStatus = {
  id: string;
  label: string;
  status: "granted" | "missing" | "not_checked" | "unsupported" | string;
  granted: boolean | null;
  detail: string;
  settings_hint: string;
};

export type HostPermissionId =
  | "host.microphone.capture"
  | "host.camera.capture"
  | "host.screen.capture"
  | "host.input.pointer"
  | "host.input.keyboard"
  | "host.clipboard.*"
  | string;

export type DesktopHostPermissionStatus = {
  id: HostPermissionId;
  label?: string;
  status?: string;
  granted?: boolean | null;
  rumi_status?: string;
  rumi_granted?: boolean | null;
  os_status?: string;
  os_granted?: boolean | null;
  risk_level?: string;
  stream_allowed?: boolean | null;
  required_by_functions?: string[];
  detail?: string;
  settings_hint?: string;
};

export type HostBrokerStatus = {
  enabled: boolean;
  available?: boolean;
  status: string;
  url?: string | null;
  connection_path?: string | null;
  recovery?: string | null;
};

export type DesktopSystemInfoSource = "launcher_tauri" | "viewer_tauri" | "viewer_broker" | "fallback" | string;

export type DesktopSystemInfo = {
  source: DesktopSystemInfoSource;
  reliable: boolean;
  app_name: string;
  display_version: string;
  launcher_version?: string;
  launcher_tauri?: boolean;
  viewer_tauri?: boolean;
  viewer_version: string;
  build_channel: string;
  platform: string;
  platform_release: string;
  permission_subject?: string;
  host_broker?: HostBrokerStatus;
  host_permissions?: DesktopHostPermissionStatus[] | Record<string, DesktopHostPermissionStatus>;
  permissions: DesktopPermissionStatus[];
};

type TauriInvoke = <T>(command: string, args?: Record<string, unknown>) => Promise<T>;

function getTauriInvoke(): TauriInvoke | null {
  const maybeWindow = window as Window & {
    __TAURI__?: {
      core?: {
        invoke?: TauriInvoke;
      };
    };
  };
  const invoke = maybeWindow.__TAURI__?.core?.invoke;
  return typeof invoke === "function" ? invoke : null;
}

function isResponseShape(value: unknown): value is { status: string; data: unknown } {
  if (!value || typeof value !== "object") return false;
  const obj = value as Record<string, unknown>;
  return typeof obj.status === "string" && "data" in obj;
}

function isDesktopSystemInfoShape(value: unknown): value is DesktopSystemInfo {
  if (!value || typeof value !== "object") return false;
  const obj = value as Record<string, unknown>;
  return (
    typeof obj.source === "string" &&
    typeof obj.reliable === "boolean" &&
    typeof obj.app_name === "string" &&
    typeof obj.platform === "string" &&
    Array.isArray(obj.permissions)
  );
}

export function isDesktopSystemInfoAvailable(): boolean {
  return getTauriInvoke() !== null;
}

export async function fetchDesktopSystemInfo(): Promise<DesktopSystemInfo | null> {
  const invoke = getTauriInvoke();
  if (invoke) {
    return invoke<DesktopSystemInfo>("get_desktop_system_info");
  }

  try {
    const res = await defaultspackApiFetch(defaultspackContractRoute("api/desktop-system-info"));
    if (!res.ok) return null;
    const json: unknown = await res.json();
    if (isResponseShape(json) && isDesktopSystemInfoShape(json.data)) {
      return json.data;
    }
    if (isDesktopSystemInfoShape(json)) {
      return json;
    }
  } catch {
    // Not available via HTTP either.
  }
  return null;
}
