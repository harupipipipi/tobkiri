import type { ModelProfile } from "../../lib/api";
import type { AttachedFile } from "../../renderers/types";
import {
  ambientTriggerClient,
  type AmbientAudioTranscriptionPayload,
  type AmbientAudioTranscriptionResult,
} from "../../ambient/ambientTriggerClient";

const AUDIO_CAPABILITY_KEYS = [
  "supports_audio",
  "supports_audio_input",
  "audio_input",
  "input_audio",
] as const;

function explicitAudioCapability(
  value: unknown,
  seen = new Set<object>(),
  depth = 0,
): boolean | null {
  if (!value || typeof value !== "object" || depth > 4) return null;
  if (seen.has(value)) return null;
  seen.add(value);
  const record = value as Record<string, unknown>;
  for (const key of AUDIO_CAPABILITY_KEYS) {
    if (typeof record[key] === "boolean") return record[key] as boolean;
  }
  for (const key of ["input_modalities", "modalities", "capabilities", "capability_tags"]) {
    const list = record[key];
    if (!Array.isArray(list)) continue;
    const normalized = list.map((item) => String(item).trim().toLowerCase());
    if (normalized.some((item) => item === "audio" || item === "input_audio" || item === "audio_input")) {
      return true;
    }
  }
  for (const key of ["metadata", "availability", "capability", "capabilities", "model", "defaults"]) {
    const nested = explicitAudioCapability(record[key], seen, depth + 1);
    if (nested !== null) return nested;
  }
  return null;
}

/**
 * Unknown capability is intentionally treated as unsupported. This keeps voice
 * input usable by routing it through transcription instead of sending an audio
 * block to a model which may reject it.
 */
export function modelSupportsAudioInput(profile: ModelProfile | null | undefined): boolean {
  return explicitAudioCapability(profile) === true;
}

export function isAudioAttachment(file: Pick<AttachedFile, "name" | "type">): boolean {
  if (String(file.type ?? "").toLowerCase().startsWith("audio/")) return true;
  return /\.(?:aac|aif|aiff|flac|m4a|mp3|oga|ogg|opus|wav|webm)$/i.test(file.name);
}

export function audioTranscriptFileName(name: string): string {
  const base = name.replace(/\.[^.]+$/, "").trim() || "voice";
  return `${base}-文字起こし.txt`;
}

export function transcriptAttachmentFromAudio(
  source: AttachedFile,
  transcript: string,
  now = Date.now(),
): AttachedFile {
  const content = transcript.trim();
  return {
    id: `transcript-${now}-${Math.random().toString(36).slice(2, 8)}`,
    name: audioTranscriptFileName(source.name),
    size: new TextEncoder().encode(content).byteLength,
    type: "text/plain",
    content,
    truncated: false,
  };
}

export function readableTranscriptionError(error: unknown): string {
  const raw = error instanceof Error ? error.message : String(error || "");
  if (/local auth token required|unauthorized|status\s*401/i.test(raw)) {
    return "ローカル認証が必要です。Tobkiri Launcher から開き直すか、接続設定を確認してください。";
  }
  if (/failed to fetch|networkerror|network request failed|load failed/i.test(raw)) {
    return "文字起こしサーバーへ接続できません。Tobkiri Launcher が起動中か、Launcherと同じURLで開いているか確認してください。";
  }
  if (/audio_payload_too_large|recorded audio is too large/i.test(raw)) {
    return "録音が大きすぎるため文字起こしできません。録音を短く分けて、もう一度お試しください。";
  }
  if (/origin not allowed|origin_denied/i.test(raw)) {
    return "この画面のURLからは文字起こしを利用できません。Tobkiri Launcher から開き直してください。";
  }
  if (/no configured transcription model|no_transcription_model/i.test(raw)) {
    return "文字起こしモデルが設定されていません。設定の「モデル」またはローカル文字起こしを確認してください。";
  }
  if (/local_whisper_not_configured|local transcription unavailable/i.test(raw)) {
    return "ローカル文字起こしが利用できません。Whisper の設定またはAIプロバイダー接続を確認してください。";
  }
  return raw.trim() || "音声を文字起こしできませんでした。";
}

type ComposerTranscriptionClient = {
  transcribeAudio(payload: AmbientAudioTranscriptionPayload): Promise<AmbientAudioTranscriptionResult>;
};

export type ComposerAudioTranscriptionOptions = {
  profile?: ModelProfile | null;
  language?: string;
  metadata?: Record<string, unknown>;
};

/**
 * Shared request path for automatic voice input and the manual audio-card action.
 * The narrow transcription endpoint cannot dispatch an ambient/agent action.
 */
export async function requestComposerAudioTranscript(
  file: AttachedFile,
  options: ComposerAudioTranscriptionOptions = {},
  client: ComposerTranscriptionClient = ambientTriggerClient,
): Promise<string> {
  if (!file.dataUrl) {
    throw new Error("音声データを読み込めないため、文字起こしを開始できません。");
  }
  const profile = options.profile;
  try {
    const result = await client.transcribeAudio({
      audio_data_url: file.dataUrl,
      audio_mime_type: file.type,
      audio_size: file.size,
      audio_name: file.name,
      model: profile?.qualified_model_id || profile?.profile_id,
      profile_id: profile?.profile_id,
      params: {
        language: options.language || "ja",
        model: profile?.qualified_model_id || profile?.model_id,
        profile_id: profile?.profile_id,
      },
      metadata: {
        surface: "composer",
        target_supports_audio: modelSupportsAudioInput(profile),
        ...(options.metadata ?? {}),
      },
    });
    const transcript = String(result.transcript ?? "").trim();
    if (transcript) return transcript;
    const transcription = result.transcription && typeof result.transcription === "object"
      ? result.transcription
      : {};
    throw new Error(String(
      transcription.reason
      || transcription.code
      || "音声を文字起こしできませんでした。",
    ));
  } catch (error) {
    throw new Error(readableTranscriptionError(error));
  }
}
