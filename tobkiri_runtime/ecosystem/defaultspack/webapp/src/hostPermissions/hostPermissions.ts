import type { AuthorityRequest } from "../lib/api";
import { redactDiagnosticText } from "../lib/clientDiagnostics";
import type { DesktopHostPermissionStatus, DesktopPermissionStatus, DesktopSystemInfo, HostPermissionId } from "../lib/desktopSystemInfo";
import hostPermissionRegistry from "./hostPermissionRegistry.json";

export type HostPermissionBucket = "approved" | "pending" | "missing" | "denied" | "blocked" | "unsupported" | "unknown";
export type HostPermissionRisk = "low" | "medium" | "high" | "critical" | string;

export type HostPermissionDefinition = {
  id: HostPermissionId;
  label: string;
  description: string;
  rumiAliases: string[];
  osAliases: string[];
  riskLevel: HostPermissionRisk;
  streamAllowed: boolean;
  requiredByFunctions: string[];
};

export type HostPermissionRow = {
  id: HostPermissionId;
  label: string;
  description: string;
  rumiStatus: HostPermissionBucket;
  osStatus: HostPermissionBucket;
  riskLevel: HostPermissionRisk;
  streamAllowed: boolean | null;
  requiredByFunctions: string[];
  detail?: string;
  settingsHint?: string;
  source: "desktop" | "fallback";
};

type RegistryHostPermissionDefinition = {
  label?: string;
  risk_level?: HostPermissionRisk;
  stream_allowed?: boolean;
  os_permissions?: Record<string, string[]>;
};

type HostPermissionUiMetadata = Partial<Pick<HostPermissionDefinition, "label" | "description" | "rumiAliases" | "osAliases" | "requiredByFunctions">>;

const HOST_PERMISSION_UI_METADATA: Record<string, HostPermissionUiMetadata> = {
  "host.microphone.capture": {
    label: "Microphone",
    description: "Capture microphone input for ambient voice and transcription features.",
    rumiAliases: ["host.microphone.capture", "microphone.capture"],
    osAliases: ["host.microphone.capture", "microphone.capture", "microphone", "mic"],
    requiredByFunctions: ["ambient_monitor_start", "ai_transcribe", "recording_capture"],
  },
  "host.camera.capture": {
    label: "Camera",
    description: "Capture camera frames for gesture and visual input workflows.",
    rumiAliases: ["host.camera.capture", "camera.capture"],
    osAliases: ["host.camera.capture", "camera.capture", "camera", "webcam"],
    requiredByFunctions: ["ambient_monitor_start", "recording_capture"],
  },
  "host.screen.capture": {
    label: "Screen Capture",
    description: "Read visible screen content for screenshots and computer-use context.",
    rumiAliases: ["host.screen.capture", "screen.capture", "media.screenshot", "computer.screenshot"],
    osAliases: ["host.screen.capture", "screen.capture", "screen_recording", "screen-recording", "screen", "display_capture", "media.screenshot", "computer.screenshot"],
    requiredByFunctions: ["computer_screenshot", "media_screenshot", "recording_capture"],
  },
  "host.input.pointer": {
    label: "Pointer Input",
    description: "Move or click the pointer through approved computer-use actions.",
    rumiAliases: ["host.input.pointer", "input.pointer", "computer.click", "computer.drag"],
    osAliases: ["host.input.pointer", "input.pointer", "accessibility", "input_monitoring", "pointer", "mouse"],
    requiredByFunctions: ["computer_click"],
  },
  "host.input.keyboard": {
    label: "Keyboard Input",
    description: "Type keystrokes through approved computer-use actions.",
    rumiAliases: ["host.input.keyboard", "input.keyboard", "computer.type"],
    osAliases: ["host.input.keyboard", "input.keyboard", "accessibility", "input_monitoring", "keyboard"],
    requiredByFunctions: ["computer_type"],
  },
  "host.clipboard.read": {
    label: "Clipboard Read",
    description: "Read the system clipboard when an approved media tool needs it.",
    rumiAliases: ["host.clipboard.read", "host.clipboard.*", "media.clipboard.read"],
    osAliases: ["host.clipboard.read", "host.clipboard.*", "clipboard", "clipboard.read", "media.clipboard.read"],
    requiredByFunctions: ["media_clipboard_read"],
  },
  "host.clipboard.write": {
    label: "Clipboard Write",
    description: "Write the system clipboard when an approved media tool needs it.",
    rumiAliases: ["host.clipboard.write", "media.clipboard.write"],
    osAliases: ["host.clipboard.write", "clipboard.write", "media.clipboard.write"],
    requiredByFunctions: ["media_clipboard_write"],
  },
};

const HOST_PERMISSION_REGISTRY = hostPermissionRegistry as Record<string, RegistryHostPermissionDefinition>;

const HOST_PERMISSION_DEFINITIONS: HostPermissionDefinition[] = Object.entries(HOST_PERMISSION_REGISTRY).map(([id, registryDefinition]) => {
  const metadata = HOST_PERMISSION_UI_METADATA[id] ?? {};
  const label = metadata.label ?? registryDefinition.label ?? titleizeHostPermissionId(id);
  const registryOsAliases = Object.values(registryDefinition.os_permissions ?? {}).flat();
  return {
    id: id as HostPermissionId,
    label,
    description: metadata.description ?? `${label}.`,
    rumiAliases: uniqueStrings([id, ...(metadata.rumiAliases ?? [])]),
    osAliases: uniqueStrings([id, ...registryOsAliases, ...(metadata.osAliases ?? [])]),
    riskLevel: registryDefinition.risk_level ?? "medium",
    streamAllowed: Boolean(registryDefinition.stream_allowed),
    requiredByFunctions: metadata.requiredByFunctions ?? [],
  };
});

export function hostPermissionDefinitions(): HostPermissionDefinition[] {
  return HOST_PERMISSION_DEFINITIONS;
}

export function buildHostPermissionRows(
  info: DesktopSystemInfo | null,
  authorityRequests: AuthorityRequest[] = [],
): HostPermissionRow[] {
  const hostEntries = hostPermissionEntryMap(info);
  const osPermissions = Array.isArray(info?.permissions) ? info.permissions : [];
  return HOST_PERMISSION_DEFINITIONS.map((definition) => {
    const entry = hostEntries.get(definition.id);
    const rumiStatus = normalizeRumiStatus(entry, definition, authorityRequests);
    const osStatus = normalizeOsStatus(entry, definition, osPermissions);
    return {
      id: definition.id,
      label: entry?.label || definition.label,
      description: entry?.detail || definition.description,
      rumiStatus,
      osStatus,
      riskLevel: normalizeRisk(entry?.risk_level) || definition.riskLevel,
      streamAllowed: definition.streamAllowed,
      requiredByFunctions: normalizedStringList(entry?.required_by_functions).length > 0
        ? normalizedStringList(entry?.required_by_functions)
        : definition.requiredByFunctions,
      detail: entry?.detail,
      settingsHint: entry?.settings_hint,
      source: entry ? "desktop" : "fallback",
    };
  });
}

export function hostPermissionSummary(rows: HostPermissionRow[]): { approved: number; osReady: number; total: number } {
  return {
    approved: rows.filter((row) => row.rumiStatus === "approved").length,
    osReady: rows.filter((row) => row.osStatus === "approved" || row.osStatus === "unsupported").length,
    total: rows.length,
  };
}

export function hostPermissionStatusLabel(status: HostPermissionBucket): string {
  switch (status) {
    case "approved":
      return "Approved";
    case "pending":
      return "Pending";
    case "missing":
      return "Missing";
    case "denied":
      return "Denied";
    case "blocked":
      return "Blocked";
    case "unsupported":
      return "Not required";
    default:
      return "Unknown";
  }
}

export function safeHostPermissionDiagnostic(error: unknown): string {
  const raw = error instanceof Error ? `${error.name}: ${error.message}` : String(error ?? "");
  return redactDiagnosticText(raw, 480) || "No additional technical details are available.";
}

function hostPermissionEntryMap(info: DesktopSystemInfo | null): Map<string, DesktopHostPermissionStatus> {
  const entries = new Map<string, DesktopHostPermissionStatus>();
  const raw = info?.host_permissions;
  if (Array.isArray(raw)) {
    for (const entry of raw) {
      if (entry?.id) entries.set(canonicalHostPermissionId(String(entry.id)), entry);
    }
    return entries;
  }
  if (raw && typeof raw === "object") {
    for (const [key, value] of Object.entries(raw)) {
      if (!value || typeof value !== "object") continue;
      const id = canonicalHostPermissionId(String(value.id || key));
      entries.set(id, { ...value, id });
    }
  }
  return entries;
}

function normalizeRumiStatus(
  entry: DesktopHostPermissionStatus | undefined,
  definition: HostPermissionDefinition,
  authorityRequests: AuthorityRequest[],
): HostPermissionBucket {
  const explicitGranted = readNullableBoolean(entry, ["rumi_granted", "rumiGranted", "granted", "approved"]);
  if (explicitGranted === true) return "approved";
  if (explicitGranted === false) return normalizeStatus(entry?.rumi_status || entry?.status, "missing");

  const explicitStatus = normalizeStatus(entry?.rumi_status || entry?.status, "unknown");
  if (explicitStatus !== "unknown") return explicitStatus;

  const request = latestAuthorityRequest(definition, authorityRequests);
  if (request?.status === "approved") return "approved";
  if (request?.status === "pending") return "pending";
  if (request?.status === "denied") return "denied";
  if (request?.status === "expired") return "missing";
  return "unknown";
}

function normalizeOsStatus(
  entry: DesktopHostPermissionStatus | undefined,
  definition: HostPermissionDefinition,
  permissions: DesktopPermissionStatus[],
): HostPermissionBucket {
  const explicitGranted = readNullableBoolean(entry, ["os_granted", "osGranted"]);
  if (explicitGranted === true) return "approved";
  if (explicitGranted === false) return normalizeStatus(entry?.os_status, "missing");

  const explicitStatus = normalizeStatus(entry?.os_status, "unknown");
  if (explicitStatus !== "unknown") return explicitStatus;

  const match = permissions.find((permission) => matchesAny(permission.id, definition.osAliases));
  if (match) {
    if (match.granted === true) return "approved";
    if (match.granted === false) return normalizeStatus(match.status, "missing");
    return normalizeStatus(match.status, "unknown");
  }

  if (String(definition.id).startsWith("host.clipboard.")) return "unsupported";
  return "unknown";
}

function latestAuthorityRequest(definition: HostPermissionDefinition, requests: AuthorityRequest[]): AuthorityRequest | undefined {
  const matches = requests.filter((request) => {
    const candidates = [
      request.permission_id,
      request.display_metadata?.permission_id,
      stringValue(request.resource?.permission_id),
      stringValue(request.resource?.host_permission_id),
      stringValue(request.resource?.host_action),
    ].filter(Boolean);
    return candidates.some((candidate) => matchesAny(candidate, definition.rumiAliases));
  });
  return matches.sort((a, b) => Date.parse(b.created_at || "") - Date.parse(a.created_at || ""))[0];
}

function canonicalHostPermissionId(id: string): string {
  const value = id.trim();
  const match = HOST_PERMISSION_DEFINITIONS.find((definition) => matchesAny(value, [definition.id, ...definition.rumiAliases]));
  return match?.id || value;
}

function matchesAny(value: string | undefined, aliases: string[]): boolean {
  const normalized = normalizeId(value);
  if (!normalized) return false;
  return aliases.some((alias) => normalizeId(alias) === normalized);
}

function normalizeId(value: string | undefined): string {
  return String(value ?? "").trim().toLowerCase().replace(/[_-]/g, ".");
}

function normalizeStatus(value: string | undefined, fallback: HostPermissionBucket): HostPermissionBucket {
  const status = normalizeId(value);
  if (!status) return fallback;
  if (status === "granted" || status === "approved" || status === "allowed" || status === "ok") return "approved";
  if (status === "pending" || status === "requested") return "pending";
  if (status === "missing" || status === "required" || status === "not.checked" || status === "prompt") return "missing";
  if (status === "denied" || status === "rejected") return "denied";
  if (status === "blocked" || status === "unavailable") return "blocked";
  if (status === "unsupported" || status === "not.required") return "unsupported";
  return fallback;
}

function normalizeRisk(value: string | undefined): HostPermissionRisk | null {
  const risk = String(value ?? "").trim().toLowerCase();
  return risk ? risk : null;
}

function readNullableBoolean(value: unknown, keys: string[]): boolean | null {
  if (!value || typeof value !== "object") return null;
  const obj = value as Record<string, unknown>;
  for (const key of keys) {
    const candidate = obj[key];
    if (typeof candidate === "boolean") return candidate;
    if (typeof candidate === "string") {
      const normalized = candidate.trim().toLowerCase();
      if (["true", "yes", "1", "granted", "approved", "allowed"].includes(normalized)) return true;
      if (["false", "no", "0", "missing", "denied", "blocked"].includes(normalized)) return false;
    }
  }
  return null;
}

function normalizedStringList(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.map((item) => String(item).trim()).filter(Boolean);
}

function stringValue(value: unknown): string | undefined {
  return typeof value === "string" ? value : undefined;
}

function uniqueStrings(values: string[]): string[] {
  return Array.from(new Set(values.map((value) => value.trim()).filter(Boolean)));
}

function titleizeHostPermissionId(id: string): string {
  return id
    .replace(/^host\./, "")
    .split(".")
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}
