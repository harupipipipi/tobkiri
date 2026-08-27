import test from "node:test";
import assert from "node:assert/strict";

import {
  AMBIENT_CAMERA_PERMISSION,
  AMBIENT_MIC_PERMISSION,
  ambientCopyJa,
  ambientOperationLabels,
  ambientPendingInputLabel,
  ambientRenderableMessage,
  deriveAmbientUiState,
  looksLikeAmbientAudioFilenamePlaceholder,
  osPermissionBucket,
  rumiPermissionBucket,
  type AmbientRuntimeStatus,
} from "./ambientUiState";
import type { AmbientPendingApproval, AmbientStatus } from "./ambientTriggerClient";

function status(options?: {
  rumi?: Partial<Record<string, boolean>>;
  os?: Partial<Record<string, string>>;
  enabled?: boolean;
}): AmbientStatus {
  return {
    ambient_monitor: { enabled: Boolean(options?.enabled) },
    services: {
      voice_wake_monitor: { status: options?.enabled ? "listening" : "paused" },
      gesture_wake_monitor: { status: options?.enabled ? "listening" : "paused" },
    },
    permissions: {
      rumi: {
        [AMBIENT_MIC_PERMISSION]: { granted: Boolean(options?.rumi?.[AMBIENT_MIC_PERMISSION]) },
        [AMBIENT_CAMERA_PERMISSION]: { granted: Boolean(options?.rumi?.[AMBIENT_CAMERA_PERMISSION]) },
        "ambient.trigger.dispatch": { granted: Boolean(options?.rumi?.["ambient.trigger.dispatch"]) },
      },
      os: {
        [AMBIENT_MIC_PERMISSION]: { status: options?.os?.[AMBIENT_MIC_PERMISSION] ?? "unknown" },
        [AMBIENT_CAMERA_PERMISSION]: { status: options?.os?.[AMBIENT_CAMERA_PERMISSION] ?? "unknown" },
      },
    },
  };
}

const allRumi = {
  [AMBIENT_MIC_PERMISSION]: true,
  [AMBIENT_CAMERA_PERMISSION]: true,
  "ambient.trigger.dispatch": true,
};

const allOs = {
  [AMBIENT_MIC_PERMISSION]: "granted",
  [AMBIENT_CAMERA_PERMISSION]: "granted",
};

test("deriveAmbientUiState guides first-run users to setup before showing off", () => {
  assert.equal(deriveAmbientUiState(status(), "off"), "setupNeeded");
});

test("deriveAmbientUiState separates Rumi permission setup from OS permission setup", () => {
  assert.equal(deriveAmbientUiState(status({ rumi: allRumi }), "off"), "osPermissionNeeded");
  assert.equal(deriveAmbientUiState(status({ os: allOs }), "off"), "rumiPermissionNeeded");
});

test("deriveAmbientUiState keeps first-run setup visible even when browser OS permission is denied", () => {
  const firstRunWithDeniedBrowserPermission = status({
    os: { [AMBIENT_MIC_PERMISSION]: "denied", [AMBIENT_CAMERA_PERMISSION]: "denied" },
  });

  assert.equal(deriveAmbientUiState(firstRunWithDeniedBrowserPermission, "off"), "setupNeeded");
});

test("deriveAmbientUiState distinguishes off, monitoring, recording, and sending", () => {
  const ready = status({ rumi: allRumi, os: allOs });
  const cases: Array<[AmbientRuntimeStatus, string]> = [
    ["off", "readyOff"],
    ["monitoring", "monitoring"],
    ["recording", "recording"],
    ["transcribing", "transcribing"],
    ["sending", "sending"],
  ];
  for (const [runtime, expected] of cases) {
    assert.equal(deriveAmbientUiState(ready, runtime), expected);
  }
});

test("permission buckets keep denied and blocked distinct from missing setup", () => {
  const denied = status({
    rumi: allRumi,
    os: { [AMBIENT_MIC_PERMISSION]: "denied", [AMBIENT_CAMERA_PERMISSION]: "granted" },
  });
  assert.equal(osPermissionBucket(denied, AMBIENT_MIC_PERMISSION), "denied");
  assert.equal(deriveAmbientUiState(denied, "off"), "denied");
  assert.equal(ambientCopyJa.states.denied.primary, "許可を開く");

  const blocked = status({
    rumi: { ...allRumi, [AMBIENT_CAMERA_PERMISSION]: false },
    os: allOs,
  });
  blocked.permissions.rumi[AMBIENT_CAMERA_PERMISSION].status = "blocked";
  assert.equal(rumiPermissionBucket(blocked, AMBIENT_CAMERA_PERMISSION), "blocked");
  assert.equal(deriveAmbientUiState(blocked, "off"), "blocked");
});

test("ambient copy uses concrete recording and send state labels", () => {
  assert.deepEqual(Object.values(ambientOperationLabels), [
    "録音中",
    "文字起こし中",
    "送信中",
    "承認待ち",
    "返答待ち",
    "完了",
    "失敗",
  ]);
  assert.match(ambientCopyJa.gestureShort, /OKマーク/);
  assert.match(ambientCopyJa.states.monitoring.body, /指を開くと送信前確認/);
  assert.match(ambientCopyJa.states.transcribing.headline, /録音音声を文字/);
  assert.doesNotMatch(JSON.stringify(ambientCopyJa), /指をくっつけ/);
});

test("ambient pending audio label never exposes generated audio filenames as input", () => {
  const pendingAudio = {
    request_id: "ambient_ai_send_1",
    input_preview: "音声入力: ambient-pinch-123456.webm",
    has_audio: true,
    attachment_count: 1,
  } as AmbientPendingApproval;
  const pendingTranscript = {
    ...pendingAudio,
    input_preview: "文字起こし: hello",
  } as AmbientPendingApproval;

  assert.equal(looksLikeAmbientAudioFilenamePlaceholder(pendingAudio.input_preview ?? ""), true);
  assert.equal(ambientPendingInputLabel(pendingAudio), "録音音声（文字起こし待ち）");
  assert.equal(ambientPendingInputLabel(pendingTranscript), "文字起こし: hello");
});

test("ambient renderable message keeps actionable problems and hides routine status text", () => {
  assert.equal(ambientRenderableMessage("送信中: hello をテスト送信しています。"), null);
  assert.equal(ambientRenderableMessage("文字起こし中: 録音音声を文字にしています。"), null);
  assert.equal(ambientRenderableMessage("返答待ち: 録音音声をAIに送信しました。返答を待っています。"), null);
  assert.equal(ambientRenderableMessage("完了: AIの回答が届きました。"), null);
  assert.equal(ambientRenderableMessage("待機中です。OKマークで録音開始、指を開くと送信前確認が開きます。"), null);
  assert.equal(ambientRenderableMessage("文字起こし: hello"), null);

  assert.equal(
    ambientRenderableMessage("失敗: 送信できませんでした。"),
    "失敗: 送信できませんでした。",
  );
  assert.equal(
    ambientRenderableMessage("承認待ち: Tobkiriの許可がそろってから録音できます。"),
    "承認待ち: Tobkiriの許可がそろってから録音できます。",
  );
});
