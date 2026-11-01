import assert from "node:assert/strict";
import test from "node:test";

import {
  appendVoiceTranscript,
  applyComposerVoiceTranscript,
  assertComposerMicrophoneAllowed,
  ComposerVoiceOperation,
  composerVoiceLanguage,
  composerVoiceErrorMessage,
  audioTranscriptFileName,
  isAudioAttachment,
  modelSupportsAudioInput,
  readableTranscriptionError,
  requestComposerAudioTranscript,
  shouldTranscribeVoiceInput,
  transcriptAttachmentFromAudio,
} from "./composerVoice";

test("unknown and explicitly unsupported models use the transcription bridge", () => {
  assert.equal(modelSupportsAudioInput({
    profile_id: "mimo/free",
    display_name: "MiMo",
  }), false);
  assert.equal(modelSupportsAudioInput({
    profile_id: "text/only",
    display_name: "Text only",
    metadata: { capabilities: { supports_audio_input: false } },
  }), false);
});

test("audio capability is accepted from profile metadata and modality lists", () => {
  assert.equal(modelSupportsAudioInput({
    profile_id: "native/audio",
    display_name: "Native audio",
    metadata: { capabilities: { audio_input: true } },
  }), true);
  assert.equal(modelSupportsAudioInput({
    profile_id: "native/modalities",
    display_name: "Native modalities",
    metadata: { input_modalities: ["text", "audio"] },
  }), true);
});

test("AI dictation transcribes even when the chat model accepts audio", () => {
  const audioProfile = {
    profile_id: "native/audio",
    display_name: "Native audio",
    metadata: { capabilities: { audio_input: true } },
  };

  assert.equal(shouldTranscribeVoiceInput(audioProfile, true), true);
  assert.equal(shouldTranscribeVoiceInput(audioProfile, false), false);
  assert.equal(shouldTranscribeVoiceInput(null, false), true);
});

test("dictation appends plain recognized words to the draft without submitting", () => {
  assert.equal(
    appendVoiceTranscript("既存の下書き  ", "  明日の予定を確認して。  "),
    "既存の下書き\n明日の予定を確認して。",
  );
  assert.equal(appendVoiceTranscript("", "こんにちは"), "こんにちは");
  assert.equal(appendVoiceTranscript("下書き", "   "), "下書き");
  assert.equal(appendVoiceTranscript("", "これは話した内容"), "これは話した内容");
});

test("audio attachments include MIME and common extension fallbacks", () => {
  assert.equal(isAudioAttachment({ name: "recording.bin", type: "audio/webm" }), true);
  assert.equal(isAudioAttachment({ name: "recording.m4a" }), true);
  assert.equal(isAudioAttachment({ name: "archive.zip", type: "application/zip" }), false);
});

test("audio transcription becomes a readable txt attachment", () => {
  const source = {
    id: "voice-1",
    name: "voice.webm",
    size: 42,
    type: "audio/webm",
  };
  const result = transcriptAttachmentFromAudio(source, " こんにちは。 ", 123);
  assert.equal(audioTranscriptFileName(source.name), "voice-文字起こし.txt");
  assert.equal(result.name, "voice-文字起こし.txt");
  assert.equal(result.content, "こんにちは。");
  assert.equal(result.type, "text/plain");
});

test("transcription authentication failures explain the recovery path", () => {
  assert.match(readableTranscriptionError(new Error("local auth token required")), /Launcher/);
  assert.match(readableTranscriptionError(new TypeError("Failed to fetch")), /文字起こしサーバー/);
  assert.match(readableTranscriptionError(new Error("no configured transcription model")), /文字起こしモデル/);
});

test("manual and automatic voice transcription share the narrow transcription client", async () => {
  const calls: unknown[] = [];
  const transcript = await requestComposerAudioTranscript(
    {
      id: "voice-1",
      name: "voice.webm",
      size: 42,
      type: "audio/webm",
      dataUrl: "data:audio/webm;base64,AAAA",
    },
    {
      profile: {
        profile_id: "opencode-zen/mimo-v2.5-free",
        display_name: "MiMo",
        provider_id: "opencode-zen",
        model_id: "mimo-v2.5-free",
        qualified_model_id: "opencode-zen/mimo-v2.5-free",
        supports_audio_input: false,
      },
      metadata: { action: "automatic_transcription_for_unsupported_model" },
    },
    {
      async transcribeAudio(payload) {
        calls.push(payload);
        return {
          transcript: " こんにちは ",
          transcription: { status: "ok", source: "local_whisper" },
        };
      },
    },
  );

  assert.equal(transcript, "こんにちは");
  assert.equal(calls.length, 1);
  assert.deepEqual(calls[0], {
    audio_data_url: "data:audio/webm;base64,AAAA",
    audio_mime_type: "audio/webm",
    audio_size: 42,
    audio_name: "voice.webm",
    model: "opencode-zen/mimo-v2.5-free",
    profile_id: "opencode-zen/mimo-v2.5-free",
    params: {
      language: "ja",
      model: "opencode-zen/mimo-v2.5-free",
      profile_id: "opencode-zen/mimo-v2.5-free",
    },
    metadata: {
      surface: "composer",
      target_supports_audio: false,
      action: "automatic_transcription_for_unsupported_model",
    },
  });
});

test("AI dictation uses the existing transcription response as composer text", async () => {
  const calls: unknown[] = [];
  const transcript = await requestComposerAudioTranscript(
    {
      id: "voice-2",
      name: "voice.webm",
      size: 42,
      type: "audio/webm",
      dataUrl: "data:audio/webm;base64,BBBB",
    },
    {
      profile: {
        profile_id: "native/audio",
        display_name: "Native audio",
        metadata: { capabilities: { audio_input: true } },
      },
      metadata: { action: "voice_input_transcription" },
    },
    {
      async transcribeAudio(payload) {
        calls.push(payload);
        return {
          transcript: "  音声から下書きを作ります。  ",
          transcription: { status: "ok", source: "local_whisper" },
        };
      },
    },
  );

  assert.equal(appendVoiceTranscript("確認:", transcript), "確認:\n音声から下書きを作ります。");
  assert.equal(calls.length, 1);
  assert.deepEqual(calls[0], {
    audio_data_url: "data:audio/webm;base64,BBBB",
    audio_mime_type: "audio/webm",
    audio_size: 42,
    audio_name: "voice.webm",
    model: "native/audio",
    profile_id: "native/audio",
    params: {
      language: "ja",
      model: undefined,
      profile_id: "native/audio",
    },
    metadata: {
      surface: "composer",
      target_supports_audio: true,
      action: "voice_input_transcription",
    },
  });
});

test("transcription failures leave attachment ownership to the caller", async () => {
  const source = {
    id: "voice-1",
    name: "voice.webm",
    size: 42,
    type: "audio/webm",
    dataUrl: "data:audio/webm;base64,AAAA",
  };
  await assert.rejects(
    requestComposerAudioTranscript(source, {}, {
      async transcribeAudio() {
        throw new TypeError("Failed to fetch");
      },
    }),
    /文字起こしサーバー/,
  );
  assert.equal(source.name, "voice.webm");
  assert.equal(source.dataUrl, "data:audio/webm;base64,AAAA");
});


test("reviewable voice insertion preserves draft whitespace and selected range", () => {
  assert.deepEqual(applyComposerVoiceTranscript("before selected after  ", " spoken ", "insert", { start: 7, end: 15 }), { value: "before spoken after  ", cursor: 13 });
  assert.deepEqual(applyComposerVoiceTranscript("draft  ", "spoken", "append", { start: 0, end: 0 }), { value: "draft  \nspoken", cursor: 14 });
  assert.deepEqual(applyComposerVoiceTranscript("draft", "  ", "replace", { start: 2, end: 4 }), { value: "draft", cursor: 4 });
});

test("voice language validation and silence/permission errors are actionable", () => {
  assert.equal(composerVoiceLanguage("ja-JP", "en-US"), "ja-JP");
  assert.equal(composerVoiceLanguage("invalid locale", ""), "en-US");
  assert.match(composerVoiceErrorMessage("not-allowed"), /permission was denied/);
  assert.match(composerVoiceErrorMessage("no-speech"), /No speech/);
  assert.doesNotMatch(readableTranscriptionError(new Error("secret-token=private-server-trace")), /private-server|secret-token/);
});

test("microphone Host permission is read-only and missing permission fails closed", async () => {
  let reads = 0;
  const client = { async status() { reads += 1; return { permissions: { rumi: {} } }; } };
  await assert.rejects(assertComposerMicrophoneAllowed(client), { name: "ComposerMicrophonePermissionError" });
  assert.equal(reads, 1);
  await assertComposerMicrophoneAllowed({ async status() { return { permissions: { rumi: { "host.microphone.capture": { granted: true } } } }; } });
});

test("cancelled or replaced voice attempts dispose late microphone recorders", async () => {
  const operation = new ComposerVoiceOperation();
  const generation = operation.next();
  let finish!: (value: { cancel(): void }) => void;
  let stopped = 0;
  const result = operation.settle(generation, new Promise<{ cancel(): void }>((resolve) => { finish = resolve; }), (recorder) => recorder.cancel());
  operation.invalidate();
  finish({ cancel() { stopped += 1; } });
  assert.equal(await result, null);
  assert.equal(stopped, 1);
  const next = operation.next();
  assert.equal(await operation.settle(next, Promise.resolve("reviewed transcript")), "reviewed transcript");
  operation.next();
  assert.equal(await operation.settle(next, Promise.resolve("stale transcript")), null);
});

test("Profile and conversation scope changes discard late transcript and recorder completions", async () => {
  const operation = new ComposerVoiceOperation();
  operation.bindScope("profile-a/conversation-a");
  const generation = operation.next();
  let finish!: (value: string) => void;
  const transcript = operation.settle(generation, new Promise<string>((resolve) => { finish = resolve; }));
  operation.bindScope("profile-b/conversation-b");
  finish("late words from conversation-a");
  assert.equal(await transcript, null);
  const token = operation.token();
  operation.bindScope("profile-b/conversation-b");
  assert.equal(operation.isCurrent(token), true);
});

test("cancellation restores the original selection without replacing a changed draft", async () => {
  const { restoreComposerVoiceSelection } = await import("./composerVoice");
  const draft = "original selected draft  ";
  assert.deepEqual(restoreComposerVoiceSelection(draft, draft, { start: 9, end: 17 }), { start: 9, end: 17 });
  assert.equal(restoreComposerVoiceSelection(draft, "new conversation draft", { start: 9, end: 17 }), null);
  assert.equal(draft, "original selected draft  ");
});

test("an empty STT response reports silence and never becomes a review transcript", async () => {
  await assert.rejects(requestComposerAudioTranscript({ id: "silent", name: "silent.webm", size: 20, type: "audio/webm", dataUrl: "data:audio/webm;base64,AAAA" }, {}, { async transcribeAudio() { return { transcript: "   " }; } }), /No speech/);
});
