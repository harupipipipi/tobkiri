import {
  Component,
  Fragment,
  Suspense,
  lazy,
  useState,
  type ComponentType,
  type ReactNode,
} from "react";

import type { ShellRenderer } from "../lib/api";
import { reportClientDiagnosticResult } from "../lib/clientDiagnostics";
import { layerClassName } from "../ui/layers/layerTokens";

const TRUSTED_RENDERER_PREFIXES = [
  "/static/renderers/",
  "/static/assets/renderers/",
] as const;

const quarantinedRendererModules = new Set<string>();
const quarantinedRendererFailures = new Map<string, RendererFailureReason>();

export type RendererFailureReason = "import_failed" | "invalid_export" | "render_failed";

class RendererLoadError extends Error {
  constructor(readonly reason: Exclude<RendererFailureReason, "render_failed">) {
    super(reason);
    this.name = "RendererLoadError";
  }
}

export function isTrustedLocalRendererModule(modulePath: string | undefined): modulePath is string {
  if (!modulePath || typeof window === "undefined") return false;
  try {
    const url = new URL(modulePath, window.location.origin);
    if (url.origin !== window.location.origin) return false;
    if (url.username || url.password || url.search || url.hash) return false;
    if (!url.pathname.endsWith(".js") || url.pathname.includes("%")) return false;
    return TRUSTED_RENDERER_PREFIXES.some((prefix) => url.pathname.startsWith(prefix));
  } catch {
    return false;
  }
}

export function hasVerifiedBuiltinRendererProvenance(
  renderer: ShellRenderer | null | undefined,
): boolean {
  if (!renderer) return false;
  const provenance = renderer.provenance;
  return renderer.verified === true
    && provenance?.source === "builtin"
    && /^[a-f0-9]{64}$/i.test(String(provenance.content_hash ?? ""))
    && String(provenance.build_id ?? "").trim().length > 0;
}

export function isRendererModuleQuarantined(modulePath: string): boolean {
  return quarantinedRendererModules.has(modulePath);
}

export function resetRendererQuarantineForTests(): void {
  quarantinedRendererModules.clear();
  quarantinedRendererFailures.clear();
}

export function rendererSafeModeEnabled(search?: string): boolean {
  const effectiveSearch = search ?? (typeof window === "undefined" ? "" : window.location.search);
  try {
    return new URLSearchParams(effectiveSearch).get("safe_mode") === "1";
  } catch {
    return false;
  }
}

export function rendererSafeModePath(currentHref: string): string {
  const origin = typeof window === "undefined" ? "http://tobkiri.local" : window.location.origin;
  const url = new URL(currentHref, origin);
  url.searchParams.set("safe_mode", "1");
  return `${url.pathname}${url.search}${url.hash}`;
}

function safeRendererLabel(value: unknown, fallback: string): string {
  const label = String(value ?? "")
    .trim()
    .replace(/[^a-zA-Z0-9._:-]/g, "_")
    .slice(0, 80);
  return label || fallback;
}

function rendererModuleLabel(modulePath: string): string {
  try {
    const pathname = new URL(modulePath, "http://tobkiri.local").pathname;
    return safeRendererLabel(pathname.split("/").pop(), "builtin-renderer.js");
  } catch {
    return "builtin-renderer.js";
  }
}

function quarantineRenderer(modulePath: string, reason: RendererFailureReason): void {
  quarantinedRendererModules.add(modulePath);
  quarantinedRendererFailures.set(modulePath, reason);
}

function rendererFailureReason(error: Error): RendererFailureReason {
  if (error instanceof RendererLoadError) return error.reason;
  if (error.name === "RendererLoadError" && error.message === "import_failed") return "import_failed";
  if (error.name === "RendererLoadError" && error.message === "invalid_export") return "invalid_export";
  return "render_failed";
}

function retryRenderer(modulePath: string): void {
  quarantinedRendererModules.delete(modulePath);
  quarantinedRendererFailures.delete(modulePath);
}

export function RendererRecoveryNotice({
  renderer,
  reason,
  onRetry,
  onDisable,
  onSafeMode,
}: {
  renderer: ShellRenderer;
  reason: RendererFailureReason;
  onRetry: () => void;
  onDisable: () => void;
  onSafeMode: () => void;
}) {
  const rendererId = safeRendererLabel(renderer.id, "builtin-renderer");
  const source = safeRendererLabel(renderer.provenance?.source, "builtin");
  const buildId = safeRendererLabel(renderer.provenance?.build_id, "unknown-build");
  return (
    <aside
      data-testid={`renderer-recovery-${rendererId}`}
      role="status"
      aria-live="polite"
      className={`${layerClassName.toast} fixed bottom-4 right-4 w-[min(24rem,calc(100vw-2rem))] rounded-xl border border-amber-400/35 bg-zinc-950/95 p-3 text-xs text-zinc-200 shadow-2xl backdrop-blur motion-reduce:transition-none`}
    >
      <p className="font-semibold text-amber-100">拡張表示を停止し、標準表示へ戻しました</p>
      <p className="mt-1 leading-5 text-zinc-400">
        この領域だけを隔離しました。他の画面と入力内容はそのまま利用できます。
      </p>
      <details className="mt-2">
        <summary className="cursor-pointer text-zinc-300 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-amber-300">
          安全な診断情報
        </summary>
        <dl className="mt-1 grid grid-cols-[auto_1fr] gap-x-2 gap-y-1 text-zinc-400">
          <dt>Renderer</dt><dd>{rendererId}</dd>
          <dt>Module</dt><dd>{rendererModuleLabel(renderer.module ?? "")}</dd>
          <dt>Source</dt><dd>{source}</dd>
          <dt>Build</dt><dd>{buildId}</dd>
          <dt>Failure</dt><dd>{reason}</dd>
        </dl>
      </details>
      <div className="mt-3 flex flex-wrap gap-2">
        <button type="button" onClick={onRetry} className="rounded-md bg-amber-100 px-2.5 py-1.5 font-semibold text-zinc-950 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-amber-300">
          再試行
        </button>
        <button type="button" onClick={onDisable} className="rounded-md border border-zinc-700 px-2.5 py-1.5 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-zinc-300">
          このセッションでは無効のまま使う
        </button>
        <button type="button" onClick={onSafeMode} className="rounded-md border border-amber-500/40 px-2.5 py-1.5 text-amber-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-amber-300">
          セーフモードで再読込
        </button>
      </div>
    </aside>
  );
}

export function loadTrustedRenderer<T extends object>(
  renderer: ShellRenderer | null | undefined,
  fallback: ComponentType<T>,
): ComponentType<T> {
  if (
    renderer?.trust !== "local"
    || !hasVerifiedBuiltinRendererProvenance(renderer)
    || !isTrustedLocalRendererModule(renderer.module)
  ) {
    return fallback;
  }

  const trustedRenderer = renderer;
  const modulePath = renderer.module;
  const exportName = renderer.export || "default";
  const LoadedRenderer = lazy(async () => {
    let module: Record<string, unknown>;
    try {
      module = await import(/* @vite-ignore */ modulePath) as Record<string, unknown>;
    } catch {
      quarantinedRendererFailures.set(modulePath, "import_failed");
      throw new RendererLoadError("import_failed");
    }
    const loaded = module[exportName];
    if (typeof loaded !== "function") {
      quarantinedRendererFailures.set(modulePath, "invalid_export");
      throw new RendererLoadError("invalid_export");
    }
    return { default: loaded as ComponentType<T> };
  });

  function VerifiedRendererRegion(props: T) {
    const [noticeDismissed, setNoticeDismissed] = useState(false);
    const [failureReason, setFailureReason] = useState<RendererFailureReason>(
      () => quarantinedRendererFailures.get(modulePath) ?? "render_failed",
    );
    const Fallback = fallback;
    const retry = () => {
      retryRenderer(modulePath);
      setNoticeDismissed(false);
      window.location.reload();
    };
    const safeMode = () => {
      window.location.assign(rendererSafeModePath(window.location.href));
    };
    const recoveryNode = (
      <Fragment>
        <Fallback {...props} />
        {!noticeDismissed && (
          <RendererRecoveryNotice
            renderer={trustedRenderer}
            reason={failureReason}
            onRetry={retry}
            onDisable={() => setNoticeDismissed(true)}
            onSafeMode={safeMode}
          />
        )}
      </Fragment>
    );
    if (quarantinedRendererModules.has(modulePath)) return recoveryNode;
    return (
      <RendererBoundary
        fallback={recoveryNode}
        onError={(error) => {
          const reason = rendererFailureReason(error);
          const rendererId = safeRendererLabel(trustedRenderer.id, "builtin-renderer");
          const source = safeRendererLabel(trustedRenderer.provenance?.source, "builtin");
          const buildId = safeRendererLabel(trustedRenderer.provenance?.build_id, "unknown-build");
          quarantineRenderer(modulePath, reason);
          setFailureReason(reason);
          void reportClientDiagnosticResult({
            source: "react.renderer_boundary",
            category: "renderer_failure",
            level: "error",
            message: "Verified builtin renderer failed",
            detail: {
              error_name: "VerifiedRendererFailure",
              error_code: reason,
              reason_type: `${rendererId}:${source}:${buildId}`,
              route: modulePath,
            },
          });
        }}
      >
        <LoadedRenderer {...props} />
      </RendererBoundary>
    );
  }

  VerifiedRendererRegion.displayName = `VerifiedRendererRegion(${trustedRenderer.id || exportName})`;
  return VerifiedRendererRegion;
}

type RendererErrorBoundaryProps = {
  fallback?: ReactNode;
  onError?: (error: Error) => void;
  children: ReactNode;
};

type RendererErrorBoundaryState = {
  failed: boolean;
};

export class RendererErrorBoundary extends Component<RendererErrorBoundaryProps, RendererErrorBoundaryState> {
  state: RendererErrorBoundaryState = { failed: false };

  static getDerivedStateFromError(): RendererErrorBoundaryState {
    return { failed: true };
  }

  componentDidCatch(error: Error): void {
    this.props.onError?.(error);
  }

  render() {
    if (this.state.failed) {
      return this.props.fallback ?? null;
    }
    return this.props.children;
  }
}

export function RendererBoundary({
  children,
  fallback = null,
  onError,
}: {
  children: ReactNode;
  fallback?: ReactNode;
  onError?: (error: Error) => void;
}) {
  return (
    <RendererErrorBoundary fallback={fallback} onError={onError}>
      <Suspense fallback={fallback}>{children}</Suspense>
    </RendererErrorBoundary>
  );
}
