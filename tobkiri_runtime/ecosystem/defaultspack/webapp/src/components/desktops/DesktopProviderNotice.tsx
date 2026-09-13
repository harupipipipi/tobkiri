import { AlertTriangle, Clipboard, RefreshCw, Settings2 } from "lucide-react";

import { cn } from "../../lib/cn";
import type { RuntimeAvailability } from "../../features/sandboxes/runtimeStatus";
import { providerLabel, providerStatusTone } from "../../features/sandboxes/runtimeStatus";
import type { RuntimeOperation } from "../../features/sandboxes/types";
import { ErrorNotice } from "../ErrorNotice";
import { RuntimeSetupDialog } from "./RuntimeSetupDialog";

type DesktopProviderNoticeProps = {
  availability: RuntimeAvailability;
  operation: RuntimeOperation | null;
  doctorLoading?: boolean;
  setupLoading?: boolean;
  operationCancelLoading?: boolean;
  onSetup: () => void;
  onDoctor: () => void;
  onCancelOperation?: () => void;
  onCopyDiagnostics: () => void;
};

function providerToneClassName(tone: "success" | "warning" | "danger" | "idle") {
  if (tone === "success") return "border-emerald-500/25 bg-emerald-500/10 text-emerald-200";
  if (tone === "warning") return "border-amber-500/25 bg-amber-500/10 text-amber-200";
  if (tone === "danger") return "border-red-500/25 bg-red-500/10 text-red-200";
  return "border-zinc-800 bg-zinc-900/60 text-zinc-400";
}

export function DesktopProviderNotice({
  availability,
  operation,
  doctorLoading = false,
  setupLoading = false,
  operationCancelLoading = false,
  onSetup,
  onDoctor,
  onCancelOperation,
  onCopyDiagnostics,
}: DesktopProviderNoticeProps) {
  const showSetup = availability.status !== "ready";
  const availabilityDetails = [
    availability.message,
    ...availability.missing.flatMap((issue) => [issue.message, issue.remediation].filter(Boolean)),
  ].join("\n\n");

  return (
    <section className={cn(
      "rounded-lg border p-3",
      showSetup ? "border-amber-500/25 bg-amber-500/[0.06]" : "border-zinc-800 bg-zinc-950/50",
    )}>
      <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
        {showSetup ? (
          <ErrorNotice
            className="min-w-0 flex-1"
            copyLabel="デスクトップランタイムの問題をコピー"
            copyText={availabilityDetails}
            message={`Selected provider: ${providerLabel(availability.selectedProvider)}`}
            severity="warning"
            title={availability.message}
          >
            {availability.missing.length > 0 && (
              <div className="mt-3 grid gap-1.5">
                {availability.missing.map((issue, index) => (
                  <div key={`${issue.code}-${index}`} className="rounded-md border border-amber-500/20 bg-black/25 px-2 py-1.5">
                    <p className="text-xs font-medium text-amber-50">{issue.message}</p>
                    {issue.remediation && <p className="mt-0.5 text-[11px] text-amber-100/70">{issue.remediation}</p>}
                  </div>
                ))}
              </div>
            )}
            {availability.providers.length > 0 && (
              <div className="mt-3 flex flex-wrap gap-1.5">
                {availability.providers.map((provider) => (
                  <span
                    key={provider.provider_id}
                    className={cn("rounded-md border px-2 py-1 text-[11px]", providerToneClassName(providerStatusTone(provider)))}
                    title={provider.message || provider.provider_id}
                  >
                    {providerLabel(provider)} · {provider.status}
                  </span>
                ))}
              </div>
            )}
          </ErrorNotice>
        ) : (
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <span className="flex h-7 w-7 items-center justify-center rounded-md border border-emerald-500/30 bg-emerald-500/10 text-emerald-200">
                <AlertTriangle size={15} />
              </span>
              <div className="min-w-0">
                <p className="truncate text-sm font-semibold text-zinc-100">{availability.message}</p>
                <p className="truncate text-xs text-zinc-500">Selected provider: {providerLabel(availability.selectedProvider)}</p>
              </div>
            </div>
            {availability.providers.length > 0 && (
              <div className="mt-3 flex flex-wrap gap-1.5">
                {availability.providers.map((provider) => (
                  <span
                    key={provider.provider_id}
                    className={cn("rounded-md border px-2 py-1 text-[11px]", providerToneClassName(providerStatusTone(provider)))}
                    title={provider.message || provider.provider_id}
                  >
                    {providerLabel(provider)} · {provider.status}
                  </span>
                ))}
              </div>
            )}
          </div>
        )}

        <div className="flex shrink-0 flex-wrap items-center gap-1.5">
          <button
            type="button"
            onClick={onSetup}
            disabled={setupLoading}
            className="flex h-8 items-center gap-1.5 rounded-md bg-zinc-100 px-3 text-xs font-semibold text-zinc-950 transition-colors hover:bg-white disabled:cursor-wait disabled:opacity-60"
          >
            <Settings2 size={13} />
            <span>Provision guest runtime</span>
          </button>
          <button
            type="button"
            onClick={onDoctor}
            disabled={doctorLoading}
            className="flex h-8 items-center gap-1.5 rounded-md border border-zinc-800 bg-zinc-950/70 px-2 text-xs font-medium text-zinc-300 transition-colors hover:bg-zinc-900 hover:text-zinc-100 disabled:cursor-wait disabled:opacity-60"
          >
            <RefreshCw size={13} />
            <span>Run doctor again</span>
          </button>
          <button
            type="button"
            onClick={onCopyDiagnostics}
            className="flex h-8 items-center gap-1.5 rounded-md border border-zinc-800 bg-zinc-950/70 px-2 text-xs font-medium text-zinc-300 transition-colors hover:bg-zinc-900 hover:text-zinc-100"
          >
            <Clipboard size={13} />
            <span>Copy diagnostics</span>
          </button>
        </div>
      </div>

      <div className="mt-3">
        <RuntimeSetupDialog
          operation={operation}
          cancelLoading={operationCancelLoading}
          onCancel={onCancelOperation}
        />
      </div>
    </section>
  );
}
