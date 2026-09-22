import assert from "node:assert/strict";
import test from "node:test";
import {
  createVoiceRecognitionSession,
  type SpeechRecognitionLike,
  type VoiceRecognitionState,
} from "./voiceRecognitionSession";

class Recognition implements SpeechRecognitionLike {
  static latest: Recognition;
  lang = "";
  continuous = true;
  interimResults = false;
  onresult: SpeechRecognitionLike["onresult"] = null;
  onend: SpeechRecognitionLike["onend"] = null;
  onerror: SpeechRecognitionLike["onerror"] = null;
  stops = 0;
  constructor() { Recognition.latest = this; }
  start() {}
  stop() { this.stops += 1; }
}

test("a permission-error retry ignores late events from the old microphone", () => {
  const states: VoiceRecognitionState[] = [];
  const transcripts: string[] = [];
  let completions = 0;
  const callbacks = {
    onState: (state: VoiceRecognitionState) => states.push(state),
    onTranscript: (text: string) => transcripts.push(text),
    onEnd: () => { completions += 1; },
  };
  const first = createVoiceRecognitionSession(Recognition, callbacks);
  first.start();
  const old = Recognition.latest;
  const lateEnd = old.onend!;
  const lateResult = old.onresult!;
  old.onerror!({ error: "not-allowed" });
  assert.equal(states.at(-1), "permission-denied");

  first.dispose();
  const retry = createVoiceRecognitionSession(Recognition, callbacks);
  retry.start();
  lateEnd();
  lateResult({ resultIndex: 0, results: [{ isFinal: true, 0: { transcript: "stale" } }] });

  assert.equal(old.stops, 1);
  assert.equal(states.at(-1), "listening");
  assert.equal(completions, 0);
  assert.deepEqual(transcripts, []);
  retry.stop();
  assert.equal(Recognition.latest.stops, 1);
  assert.equal(states.at(-1), "stopped");
  retry.dispose();
});

test("stop keeps its feedback while final speech results are delivered", () => {
  const states: VoiceRecognitionState[] = [];
  const transcripts: string[] = [];
  const session = createVoiceRecognitionSession(Recognition, {
    onState: (state) => states.push(state),
    onTranscript: (text) => transcripts.push(text),
    onEnd: () => undefined,
  });
  session.start();
  const recognition = Recognition.latest;
  assert.equal(recognition.lang, "ja-JP");
  assert.equal(recognition.interimResults, true);
  session.stop();
  recognition.onresult!({ resultIndex: 0, results: [{ isFinal: true, 0: { transcript: "done" } }] });
  recognition.onend!();
  assert.deepEqual(transcripts, ["done"]);
  assert.deepEqual(states, ["listening", "stopped"]);
});

test("unmount cleanup releases a listening microphone and detaches callbacks", () => {
  const states: VoiceRecognitionState[] = [];
  const session = createVoiceRecognitionSession(Recognition, {
    onState: (state) => states.push(state),
    onTranscript: () => assert.fail("disposed transcript"),
    onEnd: () => assert.fail("disposed completion"),
  });
  session.start();
  const recognition = Recognition.latest;
  session.dispose();
  session.dispose();
  assert.equal(recognition.stops, 1);
  assert.equal(recognition.onend, null);
  assert.equal(recognition.onresult, null);
  assert.equal(recognition.onerror, null);
  assert.deepEqual(states, ["listening"]);
});

test("constructor and stop failures produce actionable terminal state", () => {
  const states: VoiceRecognitionState[] = [];
  const callbacks = {
    onState: (state: VoiceRecognitionState) => states.push(state),
    onTranscript: () => undefined,
    onEnd: () => undefined,
  };
  class BrokenConstructor extends Recognition {
    constructor() { super(); throw new Error("unavailable"); }
  }
  createVoiceRecognitionSession(BrokenConstructor, callbacks).start();
  assert.deepEqual(states, ["failed"]);

  const session = createVoiceRecognitionSession(Recognition, callbacks);
  session.start();
  Recognition.latest.stop = () => { throw new Error("unavailable"); };
  session.stop();
  assert.equal(states.at(-1), "failed");
  assert.doesNotThrow(() => session.dispose());
});
