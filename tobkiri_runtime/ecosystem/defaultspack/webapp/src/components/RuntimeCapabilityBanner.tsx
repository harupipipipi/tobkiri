import { Camera, Settings2, Sparkles } from "lucide-react";

export function RuntimeCapabilityBanner({
  visible,
  onSwitchToVisionModel,
  onOpenModelManager,
  onOpenToolSettings,
}: {
  visible: boolean;
  onSwitchToVisionModel?: () => void;
  onOpenModelManager?: () => void;
  onOpenToolSettings?: () => void;
}) {
  if (!visible) return null;
  return (
    <div className="mb-2 rounded-xl border border-sky-500/25 bg-sky-500/10 px-3 py-2.5 rumi-anim-fade-up">
      <div className="flex items-start gap-2">
        <div className="mt-0.5 flex h-7 w-7 flex-shrink-0 items-center justify-center rounded-lg border border-sky-400/25 bg-sky-400/10 text-sky-200">
          <Camera size={14} />
        </div>
        <div className="min-w-0 flex-1">
          <p className="text-sm font-medium text-sky-100">
            画像を送信するにはVision対応モデルを選んでください。
          </p>
          <p className="mt-1 text-xs text-sky-100/75">
            このモデルは添付画像を扱えません。Vision対応モデルへ切り替えるか、モデル設定を見直してください。
          </p>
          <div className="mt-2 flex flex-wrap gap-2">
            <button
              type="button"
              onClick={onSwitchToVisionModel}
              className="inline-flex items-center gap-1.5 rounded-lg border border-sky-300/35 bg-sky-200/10 px-2.5 py-1.5 text-xs font-medium text-sky-100 hover:bg-sky-200/15"
            >
              <Sparkles size={13} />
              Visionモデルへ切替
            </button>
            <button
              type="button"
              onClick={onOpenModelManager}
              className="rounded-lg border border-zinc-700 bg-zinc-950/40 px-2.5 py-1.5 text-xs text-zinc-200 hover:border-zinc-500"
            >
              Model設定
            </button>
            <button
              type="button"
              onClick={onOpenToolSettings}
              className="inline-flex items-center gap-1.5 rounded-lg border border-zinc-700 bg-zinc-950/40 px-2.5 py-1.5 text-xs text-zinc-200 hover:border-zinc-500"
            >
              <Settings2 size={13} />
              Tool詳細
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
