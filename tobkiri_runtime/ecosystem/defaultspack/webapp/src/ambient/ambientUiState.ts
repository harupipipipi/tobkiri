import type { AmbientPendingApproval, AmbientPermissionId, AmbientStatus } from "./ambientTriggerClient";

export const AMBIENT_MIC_PERMISSION = "host.microphone.capture" satisfies AmbientPermissionId;
export const AMBIENT_CAMERA_PERMISSION = "host.camera.capture" satisfies AmbientPermissionId;

export const AMBIENT_REQUIRED_PERMISSIONS: AmbientPermissionId[] = [
  AMBIENT_MIC_PERMISSION,
  AMBIENT_CAMERA_PERMISSION,
  "ambient.trigger.dispatch",
];

export const AMBIENT_AUTHORITY_REQUEST_ID = "rumi_ambient_trigger_pack";

export const AMBIENT_OS_PERMISSIONS: AmbientPermissionId[] = [
  AMBIENT_MIC_PERMISSION,
  AMBIENT_CAMERA_PERMISSION,
];

export type AmbientPermissionBucket = "unknown" | "prompt" | "granted" | "denied" | "blocked";

export type AmbientRuntimeStatus =
  | "off"
  | "monitoring"
  | "recording"
  | "transcribing"
  | "sending"
  | "paused"
  | "blocked"
  | "error";

export type AmbientUiState =
  | "setupNeeded"
  | "rumiPermissionNeeded"
  | "osPermissionNeeded"
  | "readyOff"
  | "monitoring"
  | "recording"
  | "transcribing"
  | "sending"
  | "paused"
  | "denied"
  | "blocked"
  | "error";

export const ambientOperationLabels = {
  recording: "録音中",
  transcribing: "文字起こし中",
  sending: "送信中",
  approvalPending: "承認待ち",
  waitingResponse: "返答待ち",
  done: "完了",
  failed: "失敗",
} as const;

type AmbientStateCopy = {
  badge: string;
  headline: string;
  body: string;
  primary: string;
  tone: "blue" | "emerald" | "red" | "purple" | "zinc";
};

export type AmbientVisualIcon = "alert" | "hand" | "loader" | "mic" | "play" | "radio" | "square" | "video" | "x";

type AmbientStateVisual = {
  glyphIcon: AmbientVisualIcon;
  glyphClass: string;
  primaryIcon: AmbientVisualIcon;
  primaryButtonClass: string;
  badgeClass: string;
};

export const ambientPermissionLabels: Record<string, string> = {
  [AMBIENT_MIC_PERMISSION]: "マイク入力を使う",
  [AMBIENT_CAMERA_PERMISSION]: "手の動きを見る",
  "ambient.trigger.dispatch": "音声をAIに送る",
};

export const ambientCopyJa = {
  title: "合図待ち",
  subtitle: "指で録音",
  gestureShort: "OKマークで録音開始、指を開くと送信します。",
  privacyShort: "音声・映像は保存しません",
  auditShort: "履歴には使った時刻と結果だけ残します",
  states: {
    setupNeeded: {
      badge: "許可が必要",
      headline: "Tobkiriでの利用許可が必要です",
      body: "許可後にMacのマイク・カメラを確認します",
      primary: "Tobkiriで許可",
      tone: "blue",
    },
    rumiPermissionNeeded: {
      badge: "許可が必要",
      headline: "Tobkiriでの利用許可が必要です",
      body: "入力の入口をTobkiriに許可します",
      primary: "Tobkiriで許可",
      tone: "blue",
    },
    osPermissionNeeded: {
      badge: "Mac許可が必要",
      headline: "Macのマイク/カメラ許可が必要です",
      body: "Tobkiri側の許可は済んでいます",
      primary: "マイク・カメラを許可",
      tone: "blue",
    },
    readyOff: {
      badge: "停止中",
      headline: "合図待ちは停止中です",
      body: "OKマークで録音開始、指を開くと送信します",
      primary: "合図待ちを開始",
      tone: "zinc",
    },
    monitoring: {
      badge: "使用中",
      headline: "合図を待っています",
      body: "OKマークで録音開始、指を開くと送信します",
      primary: "停止する",
      tone: "emerald",
    },
    recording: {
      badge: "録音中",
      headline: "録音中。OKマークを崩すと送ります。",
      body: "録音データは保存しません",
      primary: "キャンセル",
      tone: "red",
    },
    transcribing: {
      badge: "文字起こし中",
      headline: "録音音声を文字にしています",
      body: "文字起こし後にAIへ送ります",
      primary: "文字起こし中...",
      tone: "purple",
    },
    sending: {
      badge: "送信中",
      headline: "音声をAIに送っています",
      body: "送信後、待機に戻ります",
      primary: "送信中...",
      tone: "purple",
    },
    paused: {
      badge: "一時停止中",
      headline: "一時停止しています",
      body: "再開すると合図待ちに戻ります",
      primary: "合図待ちを再開",
      tone: "zinc",
    },
    denied: {
      badge: "許可が拒否されています",
      headline: "マイクまたはカメラが拒否されています",
      body: "設定から許可してください。許可後はOKマークで録音します",
      primary: "許可を開く",
      tone: "red",
    },
    blocked: {
      badge: "利用できません",
      headline: "この環境では利用できません",
      body: "カメラ・マイク・ブラウザ設定を確認してください",
      primary: "解決方法を見る",
      tone: "red",
    },
    error: {
      badge: "エラー",
      headline: "問題が発生しました",
      body: "状態を確認して、もう一度お試しください",
      primary: "再確認",
      tone: "red",
    },
  } satisfies Record<AmbientUiState, AmbientStateCopy>,
};

export const ambientStateVisuals = {
  setupNeeded: {
    glyphIcon: "hand",
    glyphClass: "border-sky-400/35 bg-sky-400/10 text-sky-100",
    primaryIcon: "hand",
    primaryButtonClass: "bg-sky-300 text-zinc-950 hover:bg-sky-200",
    badgeClass: "border-sky-400/30 bg-sky-400/10 text-sky-100",
  },
  rumiPermissionNeeded: {
    glyphIcon: "hand",
    glyphClass: "border-sky-400/35 bg-sky-400/10 text-sky-100",
    primaryIcon: "hand",
    primaryButtonClass: "bg-sky-300 text-zinc-950 hover:bg-sky-200",
    badgeClass: "border-sky-400/30 bg-sky-400/10 text-sky-100",
  },
  osPermissionNeeded: {
    glyphIcon: "hand",
    glyphClass: "border-sky-400/35 bg-sky-400/10 text-sky-100",
    primaryIcon: "video",
    primaryButtonClass: "bg-sky-300 text-zinc-950 hover:bg-sky-200",
    badgeClass: "border-sky-400/30 bg-sky-400/10 text-sky-100",
  },
  readyOff: {
    glyphIcon: "radio",
    glyphClass: "border-zinc-800 bg-zinc-900 text-zinc-300",
    primaryIcon: "play",
    primaryButtonClass: "bg-zinc-100 text-zinc-950 hover:bg-white",
    badgeClass: "border-zinc-800 bg-zinc-900 text-zinc-300",
  },
  monitoring: {
    glyphIcon: "hand",
    glyphClass: "border-emerald-400/35 bg-emerald-400/10 text-emerald-100",
    primaryIcon: "square",
    primaryButtonClass: "border border-zinc-800 bg-zinc-900 text-zinc-100 hover:border-zinc-700 hover:bg-zinc-800",
    badgeClass: "border-emerald-400/30 bg-emerald-400/10 text-emerald-200",
  },
  recording: {
    glyphIcon: "mic",
    glyphClass: "border-red-400/40 bg-red-500/12 text-red-100",
    primaryIcon: "x",
    primaryButtonClass: "bg-red-400 text-zinc-950 hover:bg-red-300",
    badgeClass: "border-red-400/35 bg-red-500/10 text-red-100",
  },
  transcribing: {
    glyphIcon: "loader",
    glyphClass: "border-violet-400/35 bg-violet-400/10 text-violet-100",
    primaryIcon: "loader",
    primaryButtonClass: "cursor-wait bg-violet-300 text-zinc-950 opacity-80",
    badgeClass: "border-violet-400/30 bg-violet-400/10 text-violet-100",
  },
  sending: {
    glyphIcon: "loader",
    glyphClass: "border-violet-400/35 bg-violet-400/10 text-violet-100",
    primaryIcon: "loader",
    primaryButtonClass: "cursor-wait bg-violet-300 text-zinc-950 opacity-80",
    badgeClass: "border-violet-400/30 bg-violet-400/10 text-violet-100",
  },
  paused: {
    glyphIcon: "radio",
    glyphClass: "border-zinc-800 bg-zinc-900 text-zinc-300",
    primaryIcon: "play",
    primaryButtonClass: "bg-zinc-100 text-zinc-950 hover:bg-white",
    badgeClass: "border-zinc-800 bg-zinc-900 text-zinc-300",
  },
  denied: {
    glyphIcon: "alert",
    glyphClass: "border-red-400/35 bg-red-500/10 text-red-100",
    primaryIcon: "alert",
    primaryButtonClass: "bg-red-100 text-zinc-950 hover:bg-white",
    badgeClass: "border-red-400/35 bg-red-500/10 text-red-100",
  },
  blocked: {
    glyphIcon: "alert",
    glyphClass: "border-red-400/35 bg-red-500/10 text-red-100",
    primaryIcon: "alert",
    primaryButtonClass: "bg-red-100 text-zinc-950 hover:bg-white",
    badgeClass: "border-red-400/35 bg-red-500/10 text-red-100",
  },
  error: {
    glyphIcon: "alert",
    glyphClass: "border-red-400/35 bg-red-500/10 text-red-100",
    primaryIcon: "alert",
    primaryButtonClass: "bg-red-100 text-zinc-950 hover:bg-white",
    badgeClass: "border-red-400/35 bg-red-500/10 text-red-100",
  },
} satisfies Record<AmbientUiState, AmbientStateVisual>;

export function deriveAmbientUiState(
  status: AmbientStatus | null,
  runtimeStatus: AmbientRuntimeStatus,
): AmbientUiState {
  if (runtimeStatus === "error") return "error";
  if (runtimeStatus === "blocked") return "blocked";

  if (!status) return "setupNeeded";

  const rumiStatuses = AMBIENT_REQUIRED_PERMISSIONS.map((permissionId) => rumiPermissionBucket(status, permissionId));
  const osStatuses = AMBIENT_OS_PERMISSIONS.map((permissionId) => osPermissionBucket(status, permissionId));

  const hasMissingRumi = rumiStatuses.some((permission) => permission !== "granted");
  const hasMissingOs = osStatuses.some((permission) => permission !== "granted");

  if (hasMissingRumi) {
    if (rumiStatuses.includes("blocked")) return "blocked";
    if (rumiStatuses.includes("denied")) return "denied";
    return hasMissingOs ? "setupNeeded" : "rumiPermissionNeeded";
  }
  if (hasMissingOs) {
    if (osStatuses.includes("blocked")) return "blocked";
    if (osStatuses.includes("denied")) return "denied";
    return "osPermissionNeeded";
  }

  if (runtimeStatus === "transcribing") return "transcribing";
  if (runtimeStatus === "sending") return "sending";
  if (runtimeStatus === "recording") return "recording";
  if (runtimeStatus === "monitoring") return "monitoring";
  if (runtimeStatus === "paused") return "paused";

  return "readyOff";
}

export function rumiPermissionBucket(status: AmbientStatus | null, permissionId: AmbientPermissionId): AmbientPermissionBucket {
  const entry = status?.permissions.rumi[permissionId];
  if (entry?.granted) return "granted";
  return normalizePermissionStatus(entry?.status, "prompt");
}

export function osPermissionBucket(status: AmbientStatus | null, permissionId: AmbientPermissionId): AmbientPermissionBucket {
  const entry = status?.permissions.os[permissionId];
  if (entry?.granted) return "granted";
  return normalizePermissionStatus(entry?.status, "unknown");
}

export function grantedPermissionCount(status: AmbientStatus | null, permissionIds: AmbientPermissionId[], scope: "rumi" | "os"): number {
  return permissionIds.filter((permissionId) => (
    scope === "rumi"
      ? rumiPermissionBucket(status, permissionId) === "granted"
      : osPermissionBucket(status, permissionId) === "granted"
  )).length;
}

export function hasAllRumiPermissions(status: AmbientStatus | null): boolean {
  return grantedPermissionCount(status, AMBIENT_REQUIRED_PERMISSIONS, "rumi") === AMBIENT_REQUIRED_PERMISSIONS.length;
}

export function hasAllOsPermissions(status: AmbientStatus | null): boolean {
  return grantedPermissionCount(status, AMBIENT_OS_PERMISSIONS, "os") === AMBIENT_OS_PERMISSIONS.length;
}

export function permissionBucketLabel(bucket: AmbientPermissionBucket): string {
  switch (bucket) {
    case "granted":
      return "許可済み";
    case "denied":
      return "拒否";
    case "blocked":
      return "利用不可";
    case "prompt":
      return "未許可";
    default:
      return "未確認";
  }
}

export function ambientPendingInputLabel(pending: AmbientPendingApproval | null | undefined): string {
  if (!pending) return "入力内容を確認中";
  const preview = String(pending.input_preview ?? "").trim();
  if (preview && !looksLikeAmbientAudioFilenamePlaceholder(preview)) return preview;
  if (pending.has_audio) return "録音音声（文字起こし待ち）";
  if (pending.attachment_count && pending.attachment_count > 0) return "添付あり（内容確認待ち）";
  return "入力内容を確認中";
}

const ROUTINE_MESSAGE_PREFIXES = [
  ambientOperationLabels.recording,
  ambientOperationLabels.transcribing,
  ambientOperationLabels.sending,
  ambientOperationLabels.waitingResponse,
  ambientOperationLabels.done,
];

const ACTIONABLE_MESSAGE_PATTERN = /(?:失敗|できません|できない|使えません|利用できません|開始できません|処理できません|破棄できません|登録できません|検索できません|保存できません|読み込めません|開けません|見つかりません|拒否|許可|承認|必要|利用不可|未許可|エラー|問題|権限|設定|このブラウザでは)/;

export function ambientRenderableMessage(message: string | null | undefined): string | null {
  const text = String(message ?? "").trim();
  if (!text) return null;
  if (ROUTINE_MESSAGE_PREFIXES.some((prefix) => text === prefix || text.startsWith(`${prefix}:`) || text.startsWith(`${prefix}：`))) {
    return null;
  }
  if (/hello/i.test(text) || /(?:文字起こし|transcript)\s*[:：]/i.test(text)) {
    return null;
  }
  return ACTIONABLE_MESSAGE_PATTERN.test(text) ? text : null;
}

export function looksLikeAmbientAudioFilenamePlaceholder(value: string): boolean {
  const text = String(value || "").trim();
  if (!text) return false;
  const audioFile = /(?:^|[\s:：])[\w.-]*ambient-pinch-[\w.-]+\.(?:webm|wav|m4a|mp3|ogg)\b/i;
  if (audioFile.test(text)) return true;
  return /^音声入力\s*[:：]\s*.+\.(?:webm|wav|m4a|mp3|ogg)\b/i.test(text);
}

function normalizePermissionStatus(value: string | undefined, fallback: AmbientPermissionBucket): AmbientPermissionBucket {
  const status = String(value ?? "").trim().toLowerCase();
  if (status === "granted" || status === "approved" || status === "allowed") return "granted";
  if (status === "denied" || status === "rejected") return "denied";
  if (status === "blocked" || status === "unsupported" || status === "unavailable") return "blocked";
  if (status === "prompt" || status === "missing" || status === "required") return "prompt";
  return fallback;
}
