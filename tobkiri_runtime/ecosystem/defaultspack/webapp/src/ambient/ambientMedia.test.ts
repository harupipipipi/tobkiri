import test from "node:test";
import assert from "node:assert/strict";

import { settleSpeechRecognitionTranscript, startWakeListening, type SpeechRecognitionLike } from "./ambientMedia";

function fakeRecognition(overrides: Partial<SpeechRecognitionLike> = {}): SpeechRecognitionLike {
  return {
    continuous: true,
    interimResults: true,
    lang: "ja-JP",
    onresult: null,
    onerror: null,
    onend: null,
    start: () => undefined,
    stop: () => undefined,
    abort: () => undefined,
    ...overrides,
  };
}

test("settleSpeechRecognitionTranscript waits briefly for final speech recognition text", async () => {
  let transcript = "";
  const recognition = fakeRecognition({
    stop() {
      setTimeout(() => {
        transcript = "hello こんにちは";
        recognition.onend?.();
      }, 10);
    },
  });

  const settled = await settleSpeechRecognitionTranscript(
    recognition,
    () => transcript,
    { timeoutMs: 120 },
  );

  assert.equal(settled, "hello こんにちは");
});

test("settleSpeechRecognitionTranscript returns the latest transcript if recognition never ends", async () => {
  let transcript = "途中の文字起こし";
  const recognition = fakeRecognition({
    stop() {
      setTimeout(() => {
        transcript = "遅れて確定した文字起こし";
      }, 10);
    },
  });

  const settled = await settleSpeechRecognitionTranscript(
    recognition,
    () => transcript,
    { timeoutMs: 40 },
  );

  assert.equal(settled, "遅れて確定した文字起こし");
});

test("settleSpeechRecognitionTranscript aborts immediately for cancellation paths", async () => {
  let aborted = false;
  const recognition = fakeRecognition({
    abort() {
      aborted = true;
    },
  });

  const settled = await settleSpeechRecognitionTranscript(
    recognition,
    () => "録音キャンセル前の文字起こし",
    { abort: true, timeoutMs: 120 },
  );

  assert.equal(aborted, true);
  assert.equal(settled, "録音キャンセル前の文字起こし");
});

test("startWakeListening completes only after the first capture succeeds", async () => {
  let captures = 0;
  const embeddings: number[] = [];

  const stop = await startWakeListening(
    async (embedding) => {
      embeddings.push(embedding[0] ?? 0);
    },
    undefined,
    {
      captureEmbedding: async () => {
        captures += 1;
        return [captures];
      },
      retryDelayMs: 20,
    },
  );

  assert.equal(captures, 1);
  assert.deepEqual(embeddings, [1]);
  stop();
  await new Promise((resolve) => setTimeout(resolve, 40));
  assert.equal(captures, 1);
});

test("startWakeListening reports capture errors after startup", async () => {
  let captures = 0;
  let reported: unknown = null;

  const stop = await startWakeListening(
    async () => undefined,
    undefined,
    {
      captureEmbedding: async () => {
        captures += 1;
        if (captures > 1) throw new Error("mic disconnected");
        return [1];
      },
      onError: (error) => {
        reported = error;
      },
      retryDelayMs: 20,
    },
  );

  await new Promise((resolve) => setTimeout(resolve, 60));
  stop();
  assert.equal(captures, 2);
  assert.ok(reported instanceof Error);
  assert.equal((reported as Error).message, "mic disconnected");
});

test("startWakeListening reports embedding dispatch errors after startup", async () => {
  let captures = 0;
  const embeddings: number[] = [];
  let reported: unknown = null;

  const stop = await startWakeListening(
    async (embedding) => {
      embeddings.push(embedding[0] ?? 0);
      if (embedding[0] === 2) throw new Error("ambient event dispatch failed");
    },
    undefined,
    {
      captureEmbedding: async () => {
        captures += 1;
        return [captures];
      },
      onError: (error) => {
        reported = error;
      },
      retryDelayMs: 20,
    },
  );

  await new Promise((resolve) => setTimeout(resolve, 60));
  stop();
  assert.deepEqual(embeddings, [1, 2]);
  assert.equal(captures, 2);
  assert.ok(reported instanceof Error);
  assert.equal((reported as Error).message, "ambient event dispatch failed");
});

test("shared voice recorder closes microphone tracks on constructor/start/stop/cancel failures", async () => {
  const { startPinchAudioRecorder } = await import("./ambientMedia");
  const oldNavigator = Object.getOwnPropertyDescriptor(globalThis, "navigator");
  const oldRecorder = Object.getOwnPropertyDescriptor(globalThis, "MediaRecorder");
  let mode = "constructor";
  let trackStops = 0;
  class FakeRecorder {
    static isTypeSupported() { return true; }
    state = "recording";
    mimeType = "audio/webm";
    constructor() { if (mode === "constructor") throw new Error("constructor failed"); }
    addEventListener() { /* Device events are irrelevant to synchronous failures. */ }
    start() { if (mode === "start") throw new Error("start failed"); }
    stop() { if (mode === "stop" || mode === "cancel") throw new Error("device stopped"); this.state = "inactive"; }
  }
  Object.defineProperty(globalThis, "navigator", { configurable: true, value: { mediaDevices: { async getUserMedia() { return { getTracks: () => [{ stop() { trackStops += 1; } }] }; } } } });
  Object.defineProperty(globalThis, "MediaRecorder", { configurable: true, value: FakeRecorder });
  try {
    await assert.rejects(startPinchAudioRecorder(), /constructor failed/);
    assert.equal(trackStops, 1);
    mode = "start";
    await assert.rejects(startPinchAudioRecorder(), /start failed/);
    assert.equal(trackStops, 2);
    mode = "stop";
    const stopping = await startPinchAudioRecorder();
    await assert.rejects(stopping.stop(), /device stopped/);
    assert.equal(trackStops, 3);
    mode = "cancel";
    const cancelling = await startPinchAudioRecorder();
    assert.doesNotThrow(() => cancelling.cancel());
    cancelling.cancel();
    assert.equal(trackStops, 4);
  } finally {
    if (oldNavigator) Object.defineProperty(globalThis, "navigator", oldNavigator); else Reflect.deleteProperty(globalThis, "navigator");
    if (oldRecorder) Object.defineProperty(globalThis, "MediaRecorder", oldRecorder); else Reflect.deleteProperty(globalThis, "MediaRecorder");
  }
});
