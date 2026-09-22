export type SpeechRecognitionEventLike = {
  resultIndex: number;
  results: ArrayLike<{ isFinal?: boolean; 0?: { transcript?: string } }>;
};

export type SpeechRecognitionLike = {
  lang: string;
  continuous: boolean;
  interimResults: boolean;
  onresult: ((event: SpeechRecognitionEventLike) => void) | null;
  onend: (() => void) | null;
  onerror?: ((event: { error?: string; message?: string }) => void) | null;
  start: () => void;
  stop: () => void;
};

export type VoiceRecognitionState =
  | "listening" | "stopped" | "ended" | "permission-denied" | "failed";

export type VoiceRecognitionSession = {
  start: () => void;
  stop: () => void;
  dispose: () => void;
};

/** Own one microphone session so late browser events cannot affect a retry. */
export function createVoiceRecognitionSession(
  Recognition: new () => SpeechRecognitionLike,
  callbacks: {
    onTranscript: (text: string) => void;
    onState: (state: VoiceRecognitionState) => void;
    onEnd: () => void;
  },
): VoiceRecognitionSession {
  let recognition: SpeechRecognitionLike | null = null;
  let disposed = false;
  let ended = false;
  let state: VoiceRecognitionState = "ended";
  const update = (next: VoiceRecognitionState) => {
    state = next;
    if (!disposed) callbacks.onState(next);
  };

  return {
    start() {
      if (disposed || recognition || ended) return;
      try {
        recognition = new Recognition();
        recognition.lang = "ja-JP";
        recognition.continuous = false;
        recognition.interimResults = true;
        let finalTranscript = "";
        recognition.onresult = (event) => {
          if (disposed || ended) return;
          let interim = "";
          for (let index = event.resultIndex; index < event.results.length; index += 1) {
            const result = event.results[index];
            const text = result?.[0]?.transcript ?? "";
            if (result?.isFinal) finalTranscript += text;
            else interim += text;
          }
          const transcript = `${finalTranscript}${interim}`.trim();
          if (transcript) callbacks.onTranscript(transcript);
        };
        recognition.onend = () => {
          if (disposed || ended) return;
          ended = true;
          if (state === "listening") update("ended");
          callbacks.onEnd();
        };
        recognition.onerror = (event) => {
          if (disposed || ended) return;
          const error = String(event?.error || event?.message || "").toLowerCase();
          update(error.includes("not-allowed") || error.includes("permission")
            ? "permission-denied" : "failed");
        };
        update("listening");
        recognition.start();
      } catch {
        ended = true;
        update("failed");
      }
    },
    stop() {
      if (disposed || ended || !recognition) return;
      update("stopped");
      try {
        recognition.stop();
      } catch {
        update("failed");
      }
    },
    dispose() {
      if (disposed) return;
      disposed = true;
      if (!recognition) return;
      recognition.onresult = null;
      recognition.onerror = null;
      recognition.onend = null;
      if (ended) return;
      try {
        recognition.stop();
      } catch {
        // Cleanup cannot report through callbacks owned by an unmounted UI.
      }
    },
  };
}
