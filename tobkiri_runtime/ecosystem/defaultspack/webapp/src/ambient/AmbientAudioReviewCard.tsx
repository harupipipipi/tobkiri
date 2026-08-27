import { Check, Loader2, RefreshCcw, X } from "lucide-react";

import type { AmbientAudioReview } from "./ambientAudioReview";

type Props = {
  review: AmbientAudioReview;
  audioUrl: string | null;
  transcript: string;
  transcriptOnly: boolean;
  sending: boolean;
  attempted: boolean;
  onTranscriptChange: (value: string) => void;
  onTranscriptOnlyChange: (value: boolean) => void;
  onSend: () => void;
  onRerecord: () => void;
  onSave: () => void;
  onDiscard: () => void;
};

/** Render the local-only review boundary for a captured ambient recording. */
export function AmbientAudioReviewCard({
  review,
  audioUrl,
  transcript,
  transcriptOnly,
  sending,
  attempted,
  onTranscriptChange,
  onTranscriptOnlyChange,
  onSend,
  onRerecord,
  onSave,
  onDiscard,
}: Props) {
  const selectedTools = review.dispatchContext.eventPayload.tools ?? [];
  return (
    <section
      aria-label="録音の送信前確認"
      className="space-y-2 rounded-lg border border-sky-300/30 bg-sky-400/10 p-2 text-[11px] leading-5 text-zinc-100"
      data-testid="ambient-audio-review"
    >
      <div className="flex items-start justify-between gap-2">
        <div>
          <p className="text-[12px] font-semibold text-sky-100">送信前に録音を確認</p>
          <p className="text-zinc-400">まだ音声も文字起こしも端末の外へ送っていません。</p>
        </div>
        <span className="shrink-0 rounded border border-sky-200/25 px-1.5 py-0.5 text-[10px] text-sky-100">
          {formatRecordingDuration(review.recording.durationMs)}
          {" ・ "}
          {formatRecordingSize(review.recording.size)}
        </span>
      </div>
      {audioUrl ? (
        <audio
          controls
          preload="metadata"
          src={audioUrl}
          className="h-9 w-full"
          aria-label="録音を再生"
        />
      ) : (
        <p className="rounded border border-red-300/25 bg-red-400/10 px-2 py-1 text-red-100">
          この環境では録音を再生できません。送信せず、保存または破棄できます。
        </p>
      )}
      <label className="block">
        <span className="mb-1 block font-semibold text-zinc-200">文字起こし（送信前に編集できます）</span>
        <textarea
          value={transcript}
          onChange={(event) => onTranscriptChange(event.target.value)}
          disabled={sending || attempted}
          rows={3}
          className="w-full resize-y rounded-md border border-zinc-700 bg-black/35 px-2 py-1.5 text-[12px] leading-5 text-zinc-100 outline-none focus:border-sky-400"
          placeholder="文字起こしがない場合は、ここへ送信内容を入力できます。"
          data-testid="ambient-audio-review-transcript"
        />
      </label>
      <dl className="grid grid-cols-[4.5rem_minmax(0,1fr)] gap-x-2 rounded-md border border-white/10 bg-black/20 px-2 py-1.5 text-zinc-300">
        <dt className="text-zinc-500">送信先</dt>
        <dd className="min-w-0 break-words">{review.destinationSummary || "次の送信で新しいチャットを作成"}</dd>
        <dt className="text-zinc-500">モデル</dt>
        <dd className="min-w-0 break-words">{String(review.dispatchContext.eventPayload.model || "チャットの既定モデル")}</dd>
        <dt className="text-zinc-500">ツール</dt>
        <dd>{selectedTools.length ? selectedTools.join("、") : "追加ツールなし"}</dd>
        <dt className="text-zinc-500">承認</dt>
        <dd>{review.approvalRequired ? "送信後、Authority の追加承認が必要" : "この確認で送信"}</dd>
      </dl>
      <label className="flex min-h-11 items-center gap-2 rounded-md border border-white/10 bg-black/20 px-2 py-1.5">
        <input
          type="checkbox"
          checked={transcriptOnly}
          onChange={(event) => onTranscriptOnlyChange(event.target.checked)}
          disabled={sending || attempted}
        />
        <span>
          <strong className="block text-zinc-200">文字だけ送る（プライベート / 低通信量）</strong>
          <span className="text-zinc-500">録音バイトは送信せず、編集後の文字だけを送ります。</span>
        </span>
      </label>
      {attempted && !sending && (
        <p className="rounded border border-amber-300/25 bg-amber-400/10 px-2 py-1 text-amber-100">
          送信を一度開始したため内容を固定しています。再試行は同じリクエストIDを使い、二重送信を防ぎます。内容を変える場合は破棄して録り直してください。
        </p>
      )}
      {review.recording.size > 8 * 1024 * 1024 && !transcriptOnly && (
        <p className="rounded border border-amber-300/25 bg-amber-400/10 px-2 py-1 text-amber-100">
          録音が大きいため送信に時間がかかる可能性があります。文字だけ送るか、端末へ保存して録り直すこともできます。
        </p>
      )}
      <details className="rounded-md border border-white/10 bg-black/20 px-2 py-1">
        <summary className="cursor-pointer font-semibold text-zinc-300">処理・保存・再送について</summary>
        <p className="mt-1 text-zinc-400">
          送信前の録音はこの画面のメモリ内だけに保持します。送信すると、Launcher がローカル文字起こしを行うか、
          音声対応モデルでは設定済みプロバイダーへ音声を転送する場合があります。生音声はチャット本文と監査ログには保存せず、
          Authority 承認待ちでは最大5分間メモリに保持して承認・拒否・期限切れで削除します。編集した文字とAI応答はチャット履歴に残ります。
          通信結果が不明な場合は自動再送しません。
        </p>
      </details>
      <p className="text-zinc-500">
        オフラインのまま終了する場合は「端末へ保存」の後に「破棄」を選んでください。
      </p>
      <div className="grid grid-cols-2 gap-2">
        <button
          type="button"
          onClick={onSend}
          disabled={sending || (transcriptOnly && !transcript.trim())}
          className="ambient-mini-button min-h-11 border-emerald-300/35 text-emerald-100"
          data-testid="ambient-audio-review-send"
        >
          {sending ? <Loader2 size={13} className="animate-spin" /> : <Check size={13} />}
          送信
        </button>
        <button type="button" onClick={onRerecord} disabled={sending} className="ambient-mini-button min-h-11">
          <RefreshCcw size={13} />
          録り直す
        </button>
        <button type="button" onClick={onSave} disabled={sending || !audioUrl} className="ambient-mini-button min-h-11">
          端末へ保存
        </button>
        <button
          type="button"
          onClick={onDiscard}
          disabled={sending}
          className="ambient-mini-button min-h-11 border-red-300/25 text-red-100"
          data-testid="ambient-audio-review-discard"
        >
          <X size={13} />
          破棄
        </button>
      </div>
    </section>
  );
}

function formatRecordingDuration(durationMs: number): string {
  const seconds = Math.ceil(Math.max(0, durationMs) / 1000);
  const minutes = Math.floor(seconds / 60);
  return `${String(minutes).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}`;
}

function formatRecordingSize(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) return "0 KB";
  if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}
