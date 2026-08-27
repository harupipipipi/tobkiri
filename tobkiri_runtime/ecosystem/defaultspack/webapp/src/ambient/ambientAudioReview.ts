import type { AmbientEventPayload } from "./ambientTriggerClient";
import type { AmbientDispatchTemplateContext } from "./ambientDispatchContext";

export type AmbientAudioReviewRecording = {
  dataUrl: string;
  mimeType: string;
  extension: string;
  size: number;
  durationMs: number;
};

export type AmbientAudioReview = {
  requestId: string;
  recording: AmbientAudioReviewRecording;
  transcript: string;
  requestedConversationId: string | null;
  previousAssistantMessageId: string | null;
  destinationSummary: string;
  approvalRequired: boolean;
  confidence: number;
  hand: string;
  normalizedDistance: number;
  releaseReason: string;
  dispatchContext: AmbientDispatchTemplateContext;
  capturedAt: number;
};

type CreateAmbientAudioReviewInput = Omit<AmbientAudioReview, "requestId" | "dispatchContext"> & {
  dispatchContext: AmbientDispatchTemplateContext;
  requestId?: string;
};

export function createAmbientAudioReview(input: CreateAmbientAudioReviewInput): AmbientAudioReview {
  return {
    ...input,
    requestId: input.requestId || createAmbientAudioRequestId(),
    transcript: input.transcript.trim(),
    dispatchContext: cloneDispatchContext(input.dispatchContext),
  };
}

export function buildAmbientAudioReviewPayload(
  review: AmbientAudioReview,
  options: { transcript: string; transcriptOnly: boolean },
): AmbientEventPayload {
  const transcript = options.transcript.trim();
  const recording = review.recording;
  return {
    source: "camera",
    trigger: "pinch",
    mode: "dispatch_audio",
    action_id: "chat.message",
    event_id: review.requestId,
    ...review.dispatchContext.eventPayload,
    ...(transcript ? { input_text: transcript } : {}),
    conversation_id: review.requestedConversationId || undefined,
    confidence: review.confidence,
    duration_ms: recording.durationMs,
    audio_mime_type: recording.mimeType,
    audio_size: recording.size,
    audio_name: `ok-mark-recording.${recording.extension}`,
    metadata: {
      panel: "ambient_mini_window",
      hand: review.hand,
      normalized_distance: review.normalizedDistance,
      hold_to_record: true,
      review_confirmed: true,
      transcript_available: Boolean(transcript),
      transcript_only: options.transcriptOnly,
      release_reason: review.releaseReason,
      captured_at_ms: review.capturedAt,
      ...review.dispatchContext.metadata,
      ...(transcript ? { transcript_source: "user_reviewed" } : {}),
    },
    attachments: options.transcriptOnly
      ? []
      : [
        {
          id: `ambient-audio-${review.requestId}`,
          name: `ok-mark-recording.${recording.extension}`,
          type: recording.mimeType,
          size: recording.size,
          duration_ms: recording.durationMs,
          dataUrl: recording.dataUrl,
          source: "ambient.camera_pinch_hold",
          ephemeral: true,
          do_not_persist: true,
          ...(transcript
            ? {
              transcript,
              transcription: transcript,
              transcript_source: "user_reviewed",
            }
            : {}),
        },
      ],
  };
}

export function ambientAudioReviewBlob(dataUrl: string): Blob {
  const match = /^data:([^;,]+)?(;base64)?,(.*)$/s.exec(dataUrl);
  if (!match) throw new Error("録音データを読み取れませんでした。");
  const mimeType = match[1] || "application/octet-stream";
  const encoded = match[3] || "";
  const binary = match[2] ? atob(encoded) : decodeURIComponent(encoded);
  const bytes = Uint8Array.from(binary, (character) => character.charCodeAt(0));
  return new Blob([bytes], { type: mimeType });
}

function cloneDispatchContext(context: AmbientDispatchTemplateContext): AmbientDispatchTemplateContext {
  return {
    eventPayload: {
      ...context.eventPayload,
      ...(context.eventPayload.params ? { params: structuredClone(context.eventPayload.params) } : {}),
      ...(context.eventPayload.tools ? { tools: [...context.eventPayload.tools] } : {}),
    },
    metadata: structuredClone(context.metadata),
  };
}

function createAmbientAudioRequestId(): string {
  const randomId = globalThis.crypto?.randomUUID?.().replace(/-/g, "")
    || `${Date.now()}_${Math.random().toString(16).slice(2)}`;
  return `ambient_audio_review_${randomId}`;
}
