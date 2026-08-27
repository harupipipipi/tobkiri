import assert from "node:assert/strict";
import { test } from "node:test";

import {
  ambientAudioReviewBlob,
  buildAmbientAudioReviewPayload,
  createAmbientAudioReview,
} from "./ambientAudioReview";

function review() {
  return createAmbientAudioReview({
    requestId: "ambient_audio_review_test",
    recording: {
      dataUrl: "data:audio/webm;base64,SGVsbG8=",
      mimeType: "audio/webm",
      extension: "webm",
      size: 5,
      durationMs: 1200,
    },
    transcript: " background words ",
    requestedConversationId: "chat-captured",
    previousAssistantMessageId: "message-before",
    destinationSummary: "選択中のチャット",
    approvalRequired: true,
    confidence: 0.81,
    hand: "Right",
    normalizedDistance: 0.04,
    releaseReason: "released",
    dispatchContext: {
      eventPayload: {
        model: "provider/model-at-capture",
        params: { model: "provider/model-at-capture" },
        tools: ["tool.at.capture"],
      },
      metadata: { selected_model: "provider/model-at-capture" },
    },
    capturedAt: 1234,
  });
}

test("review snapshots destination and routing context", () => {
  const pending = review();
  assert.equal(pending.transcript, "background words");
  assert.equal(pending.requestedConversationId, "chat-captured");
  assert.equal(pending.dispatchContext.eventPayload.model, "provider/model-at-capture");
  assert.deepEqual(pending.dispatchContext.eventPayload.tools, ["tool.at.capture"]);
});

test("review payload uses corrected transcript and stable request id", () => {
  const payload = buildAmbientAudioReviewPayload(review(), {
    transcript: " corrected intent ",
    transcriptOnly: false,
  });
  assert.equal(payload.event_id, "ambient_audio_review_test");
  assert.equal(payload.conversation_id, "chat-captured");
  assert.equal(payload.input_text, "corrected intent");
  assert.equal(payload.model, "provider/model-at-capture");
  assert.deepEqual(payload.tools, ["tool.at.capture"]);
  assert.equal(payload.attachments?.length, 1);
  assert.equal(payload.attachments?.[0]?.transcript, "corrected intent");
  assert.equal(payload.metadata?.review_confirmed, true);
});

test("transcript-only dispatch does not include audio bytes", () => {
  const payload = buildAmbientAudioReviewPayload(review(), {
    transcript: "safe text only",
    transcriptOnly: true,
  });
  assert.deepEqual(payload.attachments, []);
  assert.equal(payload.input_text, "safe text only");
  assert.equal(payload.metadata?.transcript_only, true);
  assert.doesNotMatch(JSON.stringify(payload), /SGVsbG8/);
});

test("local playback blob decodes without network access", async () => {
  const blob = ambientAudioReviewBlob(review().recording.dataUrl);
  assert.equal(blob.type, "audio/webm");
  assert.equal(await blob.text(), "Hello");
});
