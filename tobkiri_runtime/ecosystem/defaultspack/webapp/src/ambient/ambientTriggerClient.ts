import {
  defaultspackApiFetch,
  defaultspackContractRoute,
  type DefaultspackContractRoute,
} from "../lib/api";
import type { AuthorityUiOperator, ToolSelectionRequest } from "../lib/api";

export type AmbientPermissionId = "host.microphone.capture" | "host.camera.capture" | "ambient.trigger.dispatch" | string;

export type AmbientPermissionStatus = {
  granted?: boolean;
  status?: string;
  label?: string;
  risk?: string;
  requires_user_grant?: boolean;
  os_permission_hint?: string;
  checked_at?: string | null;
};

export type AmbientServiceStatus = {
  enabled?: boolean;
  status?: "listening" | "denied" | "paused" | string;
  enrolled?: boolean;
  classifier?: string;
  detector?: string;
  action?: string;
  cooldown_ms?: number;
};

export type AmbientStatus = {
  ambient_monitor: {
    enabled: boolean;
    updated_at?: string | null;
    controls?: string[];
  };
  services: {
    voice_wake_monitor: AmbientServiceStatus;
    gesture_wake_monitor: AmbientServiceStatus;
  };
  permissions: {
    rumi: Record<AmbientPermissionId, AmbientPermissionStatus>;
    os: Record<AmbientPermissionId, AmbientPermissionStatus>;
  };
  hooks?: Record<string, { enabled?: boolean; profile?: string }>;
  privacy?: Record<string, unknown>;
  voice_enrollment?: Record<string, unknown> | null;
  last_trigger?: Record<string, unknown> | null;
  audit_tail?: Array<Record<string, unknown>>;
  allowed_actions?: string[];
  input_aliases?: Record<string, string>;
  routing?: AmbientRoutingConfig;
  pending_approval?: AmbientPendingApproval | null;
  local_transcription?: {
    status?: string;
    configured?: boolean;
    command?: string;
    command_label?: string;
    model?: string;
    model_quality?: string;
    ffmpeg?: string;
    can_convert_audio?: boolean;
    reason?: string;
  };
};

export type AmbientRoutingMode = "selected_chat" | "startup_new_chat" | "always_new_chat";

export type AmbientRoutingConfig = {
  mode?: AmbientRoutingMode | string;
  conversation_id?: string | null;
  group_enabled?: boolean | string | null;
  group_id?: string | null;
  group_title?: string | null;
  model?: string | null;
  session_conversation_id?: string | null;
  ai_send_approval_required?: boolean | string | null;
};

export type AmbientPendingApproval = {
  request_id: string;
  source?: string;
  trigger?: string;
  mode?: string;
  action_id?: string;
  input_preview?: string;
  has_text?: boolean;
  attachment_count?: number;
  has_audio?: boolean;
  conversation_id?: string | null;
  created_at?: string | null;
  expires_at?: string | null;
  pending_count?: number;
};

export type AmbientEventPayload = {
  source: "microphone" | "camera" | "hook" | string;
  trigger: "voice_wake" | "transcription_test" | "pinch" | "gesture_choice" | "approval_gesture" | "external_hook" | string;
  event_id?: string;
  confidence?: number;
  duration_ms?: number;
  mode?: "open_input" | "focus_composer" | "enroll_wake_voice" | "dispatch" | "choice_response" | "swipe_approve" | "swipe_reject" | string;
  action_id?: "chat.message" | "run.instruction" | "agent.delegate" | "defaults.console.input" | string;
  conversation_id?: string;
  input_text?: string;
  model?: string;
  profile_id?: string;
  next_action?: string;
  choice?: 2 | 3 | 4;
  decision?: "approve" | "reject" | string;
  params?: {
    model?: string;
    profile_id?: string;
    tool_selection?: ToolSelectionRequest;
    tool_policy?: Record<string, unknown>;
    [key: string]: unknown;
  };
  tools?: string[];
  metadata?: Record<string, unknown>;
  audio_embedding?: number[];
  samples?: number[];
  audio_data_url?: string;
  audio_mime_type?: string;
  audio_size?: number;
  audio_name?: string;
  attachments?: Array<Record<string, unknown>>;
};

export type AmbientAudioTranscriptionPayload = {
  audio_data_url: string;
  audio_mime_type?: string;
  audio_size?: number;
  audio_name?: string;
  model?: string;
  profile_id?: string;
  params?: {
    language?: string;
    model?: string;
    profile_id?: string;
    [key: string]: unknown;
  };
  metadata?: Record<string, unknown>;
};

export type AmbientAudioTranscriptionResult = {
  transcript?: string;
  transcription?: Record<string, unknown>;
};

export function explainAmbientNetworkFailure(error: unknown): Error {
  if (
    typeof DOMException !== "undefined"
    && error instanceof DOMException
    && error.name === "AbortError"
  ) {
    return new Error("文字起こしリクエストがキャンセルされました。");
  }
  const raw = error instanceof Error ? error.message : String(error || "");
  if (/failed to fetch|networkerror|network request failed|load failed/i.test(raw)) {
    return new Error(
      "文字起こしサーバーへ接続できません。Tobkiri Launcher が起動中か、"
      + "この画面を開いたホスト（localhost または 127.0.0.1）がLauncherのURLと一致しているか確認してください。",
    );
  }
  return error instanceof Error ? error : new Error(raw || "ambient request failed");
}

async function requestJson<T>(path: DefaultspackContractRoute, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await defaultspackApiFetch(path, {
      ...init,
      credentials: init?.credentials ?? "same-origin",
      headers: {
        "Content-Type": "application/json",
        ...(init?.headers ?? {}),
      },
    });
  } catch (error) {
    throw explainAmbientNetworkFailure(error);
  }
  const payload = await response.json().catch(() => ({}));
  if (!response.ok || payload?.status === "error") {
    const message = payload?.error?.message || payload?.error || response.statusText;
    throw new Error(String(message || "ambient request failed"));
  }
  return (payload?.data ?? payload) as T;
}

export const ambientTriggerClient = {
  status() {
    return requestJson<AmbientStatus>(defaultspackContractRoute("api/ambient/status"), { cache: "no-store" });
  },

  startMonitor(options?: { voice_wake?: boolean; gesture_pinch?: boolean }) {
    return requestJson<AmbientStatus>(defaultspackContractRoute("api/ambient/monitor/start"), {
      method: "POST",
      body: JSON.stringify(options ?? {}),
    });
  },

  stopMonitor() {
    return requestJson<AmbientStatus>(defaultspackContractRoute("api/ambient/monitor/stop"), {
      method: "POST",
      body: JSON.stringify({}),
    });
  },

  configure(routing: AmbientRoutingConfig) {
    return requestJson<AmbientStatus>(defaultspackContractRoute("api/ambient/config"), {
      method: "POST",
      body: JSON.stringify({ routing }),
    });
  },

  grantPermission(permissionId: AmbientPermissionId, options?: { osStatus?: string; uiOperator?: AuthorityUiOperator }) {
    return requestJson<AmbientStatus>(defaultspackContractRoute("api/ambient/permissions/grant"), {
      method: "POST",
      body: JSON.stringify({
        permission_id: permissionId,
        os_status: options?.osStatus,
        ui_operator: options?.uiOperator,
      }),
    });
  },

  revokePermission(permissionId: AmbientPermissionId, options?: { uiOperator?: AuthorityUiOperator }) {
    return requestJson<AmbientStatus>(defaultspackContractRoute("api/ambient/permissions/revoke"), {
      method: "POST",
      body: JSON.stringify({ permission_id: permissionId, ui_operator: options?.uiOperator }),
    });
  },

  checkOsPermissions(statuses: Record<AmbientPermissionId, string>) {
    return requestJson<AmbientStatus>(defaultspackContractRoute("api/ambient/permissions/check"), {
      method: "POST",
      body: JSON.stringify({ statuses }),
    });
  },

  submitEvent(payload: AmbientEventPayload) {
    return requestJson<Record<string, unknown>>(defaultspackContractRoute("api/ambient/events"), {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },

  transcribeAudio(payload: AmbientAudioTranscriptionPayload) {
    return requestJson<AmbientAudioTranscriptionResult>(defaultspackContractRoute("api/ambient/transcriptions"), {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },

  approvePendingApproval(requestId: string) {
    return requestJson<Record<string, unknown>>(defaultspackContractRoute("api/ambient/approval/approve"), {
      method: "POST",
      body: JSON.stringify({ request_id: requestId }),
    });
  },

  denyPendingApproval(requestId: string, reason?: string) {
    return requestJson<Record<string, unknown>>(defaultspackContractRoute("api/ambient/approval/deny"), {
      method: "POST",
      body: JSON.stringify({ request_id: requestId, reason }),
    });
  },
};
