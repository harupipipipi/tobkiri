import { ErrorNotice } from "./ErrorNotice";

const assetBaseUrl = (
  import.meta as ImportMeta & { env?: { BASE_URL?: string } }
).env?.BASE_URL || "/static/";

export const TOBKIRI_LOADING_ANIMATION_URL =
  `${assetBaseUrl}assets/tobkiri-startup-blade-cut.svg`;

export const TOBKIRI_LOADING_LABEL = "インターフェース本体を読み込んでいます…";
export const TOBKIRI_STARTUP_ERROR_LABEL =
  "正規のTobkiriランタイムへ接続できませんでした。";

export type TobkiriLoadingStep = {
  id: string;
  label: string;
  status: "pending" | "loading" | "ready" | "error";
};

type TobkiriLoadingScreenProps = {
  steps?: readonly TobkiriLoadingStep[];
  error?: string | null;
  onRetry?: () => void;
};

/**
 * Brand-aligned startup state shared by the shell bootstrap boundaries.
 *
 * The animated SVG is the same local asset used by Tobkiri Launcher. People
 * who prefer reduced motion see a stable wordmark instead of the animation.
 */
export function TobkiriLoadingScreen({
  steps = [],
  error = null,
  onRetry,
}: TobkiriLoadingScreenProps = {}) {
  const activeStep = steps.find((step) => step.status === "loading");
  const accessibleLabel = error
    ? "Tobkiriの起動準備を完了できませんでした"
    : activeStep?.label ?? TOBKIRI_LOADING_LABEL;

  return (
    <main
      aria-label={accessibleLabel}
      aria-live="polite"
      className="flex h-full min-h-screen w-full items-center justify-center overflow-hidden bg-[#09090b] px-6 py-12 text-zinc-100"
      data-tobkiri-loading-screen=""
      role="status"
    >
      <div className="flex w-full max-w-xl flex-col items-center gap-4 text-center">
        <img
          alt=""
          className="aspect-[2/1] w-full object-contain mix-blend-screen invert motion-reduce:hidden"
          data-loading-scene="launcher"
          src={TOBKIRI_LOADING_ANIMATION_URL}
        />
        <div
          className="hidden aspect-[2/1] w-full items-center justify-center motion-reduce:flex"
          data-reduced-motion-wordmark=""
        >
          <span className="text-4xl font-semibold tracking-tight text-zinc-50">
            Tobkiri
          </span>
        </div>
        <p className="text-base font-semibold tracking-tight text-zinc-100">
          Tobkiri
        </p>
        {!error ? (
          <p className="text-sm text-zinc-300">
            {activeStep?.label ?? TOBKIRI_LOADING_LABEL}
          </p>
        ) : null}
        {!error && steps.length > 0 ? (
          <ol
            aria-label="起動準備の進行状況"
            className="mt-1 grid w-full max-w-md gap-2 text-left"
            data-startup-readiness-steps=""
          >
            {steps.map((step) => (
              <li
                className={`flex items-center gap-3 rounded-lg border px-3 py-2 text-xs ${
                  step.status === "error"
                    ? "border-red-500/30 bg-red-500/10 text-red-100"
                    : step.status === "ready"
                      ? "border-emerald-500/20 bg-emerald-500/5 text-zinc-300"
                      : step.status === "loading"
                        ? "border-sky-400/30 bg-sky-400/10 text-zinc-100"
                        : "border-zinc-800/80 bg-zinc-950/40 text-zinc-500"
                }`}
                data-startup-step={step.id}
                data-status={step.status}
                key={step.id}
              >
                <span
                  aria-hidden="true"
                  className={`h-2 w-2 flex-none rounded-full ${
                    step.status === "error"
                      ? "bg-red-400"
                      : step.status === "ready"
                        ? "bg-emerald-400"
                        : step.status === "loading"
                          ? "animate-pulse bg-sky-300 motion-reduce:animate-none"
                          : "bg-zinc-700"
                  }`}
                />
                <span>{step.label}</span>
              </li>
            ))}
          </ol>
        ) : null}
        {error ? (
          <div className="mt-1 flex w-full max-w-md flex-col items-center gap-3">
            <ErrorNotice
              className="w-full text-left text-xs leading-5"
              copyLabel="起動エラーをコピー"
              copyText={`Tobkiri Launcher startup error\n\n${error}`}
              errorIcon="startup"
              message="Tobkiri Launcherから起動し直すか、しばらく待って再試行してください。"
              messageClassName="text-zinc-300"
              title={TOBKIRI_STARTUP_ERROR_LABEL}
              titleClassName="text-red-100"
            />
            {onRetry ? (
              <button
                className="rounded-lg border border-zinc-700 bg-zinc-900 px-4 py-2 text-xs font-semibold text-zinc-100 hover:bg-zinc-800 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-zinc-400"
                onClick={onRetry}
                type="button"
              >
                再試行
              </button>
            ) : null}
            <details className="w-full text-left text-[11px] text-zinc-600">
              <summary className="cursor-pointer text-center hover:text-zinc-400">
                技術詳細
              </summary>
              <p className="mt-2 break-words rounded-lg border border-zinc-800 bg-zinc-950/60 p-3 leading-5">
                {error}
              </p>
            </details>
          </div>
        ) : null}
      </div>
    </main>
  );
}
