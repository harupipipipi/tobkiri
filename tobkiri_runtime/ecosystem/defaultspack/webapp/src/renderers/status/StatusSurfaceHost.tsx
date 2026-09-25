import {
  Activity,
  AlertTriangle,
  Boxes,
  ChevronDown,
  ChevronUp,
  CircleCheck,
  Clock3,
  Loader2,
  Pause,
  Pencil,
  Play,
  Trash2,
  Upload,
  XCircle,
} from "lucide-react";
import { useEffect, useMemo, useState, type ReactNode } from "react";

import type {
  ResolvedStatusSurface,
  ResolvedStatusSurfaceControl,
  StatusSurfaceActionRequest,
  StatusSurfaceOption,
  StatusSurfaceSlot,
} from "../../lib/statusSurfaces";

type ActionState = { status: "idle" | "pending" | "success" | "error"; message?: string };

export type StatusSurfaceHostProps = {
  surfaces: ResolvedStatusSurface[];
  slot: StatusSurfaceSlot;
  modelOptions?: StatusSurfaceOption[];
  providerOptions?: StatusSurfaceOption[];
  thinkingOptions?: StatusSurfaceOption[];
  maxVisible?: number;
  onAction?: (request: StatusSurfaceActionRequest) => Promise<unknown> | unknown;
};

const SEVERITY_STYLES: Record<ResolvedStatusSurface["severity"], string> = {
  neutral: "border-zinc-700/70 bg-zinc-950/80 text-zinc-200",
  success: "border-emerald-500/30 bg-emerald-500/10 text-emerald-100",
  warning: "border-amber-500/30 bg-amber-500/10 text-amber-100",
  error: "border-rose-500/35 bg-rose-500/10 text-rose-100",
};

function iconFor(name: string | undefined, className = "h-4 w-4"): ReactNode {
  const props = { className, "aria-hidden": true as const };
  switch (name) {
    case "warning": return <AlertTriangle {...props} />;
    case "success": return <CircleCheck {...props} />;
    case "error": return <XCircle {...props} />;
    case "pause": return <Pause {...props} />;
    case "play": return <Play {...props} />;
    case "pencil": return <Pencil {...props} />;
    case "trash": return <Trash2 {...props} />;
    case "upload": return <Upload {...props} />;
    case "build": return <Boxes {...props} />;
    case "clock": return <Clock3 {...props} />;
    default: return <Activity {...props} />;
  }
}

function formatElapsed(startedAt: string | undefined, now: number): string | null {
  if (!startedAt) return null;
  const timestamp = Date.parse(startedAt);
  if (!Number.isFinite(timestamp)) return null;
  const seconds = Math.max(0, Math.floor((now - timestamp) / 1_000));
  const hours = Math.floor(seconds / 3_600);
  const minutes = Math.floor((seconds % 3_600) / 60);
  const remainder = seconds % 60;
  return hours > 0
    ? `${hours}:${String(minutes).padStart(2, "0")}:${String(remainder).padStart(2, "0")}`
    : `${minutes}:${String(remainder).padStart(2, "0")}`;
}

function useElapsedClock(enabled: boolean): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!enabled) return undefined;
    const interval = window.setInterval(() => setNow(Date.now()), 1_000);
    return () => window.clearInterval(interval);
  }, [enabled]);
  return now;
}

function selectOptions(
  control: ResolvedStatusSurfaceControl,
  modelOptions: StatusSurfaceOption[],
  providerOptions: StatusSurfaceOption[],
  thinkingOptions: StatusSurfaceOption[],
): StatusSurfaceOption[] {
  if (control.options.length > 0) return control.options;
  if (control.type === "model_select") return modelOptions;
  if (control.type === "provider_select") return providerOptions;
  if (control.type === "thinking_select") return thinkingOptions;
  return [];
}

function actionErrorMessage(reason: unknown): string {
  if (reason instanceof Error && reason.message.trim()) return reason.message.slice(0, 300);
  return "The backend rejected this action. Displayed state was retained.";
}

function StatusControl({
  surface,
  control,
  state,
  modelOptions,
  providerOptions,
  thinkingOptions,
  onInvoke,
  onToggleDetails,
  detailsExpanded,
  actionAvailable,
}: {
  surface: ResolvedStatusSurface;
  control: ResolvedStatusSurfaceControl;
  state: ActionState;
  modelOptions: StatusSurfaceOption[];
  providerOptions: StatusSurfaceOption[];
  thinkingOptions: StatusSurfaceOption[];
  onInvoke: (control: ResolvedStatusSurfaceControl, value?: string | boolean | number | null) => void;
  onToggleDetails: () => void;
  detailsExpanded: boolean;
  actionAvailable: boolean;
}) {
  if (control.type === "expand") {
    return (
      <button
        type="button"
        className="inline-flex min-h-9 items-center gap-1.5 rounded-md border border-zinc-700/80 px-2.5 text-xs font-medium text-zinc-200 hover:bg-zinc-800/70 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-cyan-400"
        aria-expanded={detailsExpanded}
        aria-controls={`status-surface-details-${surface.id}`}
        onClick={onToggleDetails}
      >
        {detailsExpanded ? <ChevronUp size={14} aria-hidden="true" /> : <ChevronDown size={14} aria-hidden="true" />}
        {control.label}
      </button>
    );
  }

  const disabled = control.disabled || state.status === "pending" || !actionAvailable;
  const options = selectOptions(
    control,
    modelOptions,
    providerOptions,
    thinkingOptions,
  );
  if (["model_select", "provider_select", "thinking_select", "select", "menu"].includes(control.type)) {
    return (
      <label className="grid min-w-28 gap-1 text-[10px] font-medium text-zinc-400">
        <span>{control.label}</span>
        <select
          className="min-h-9 max-w-52 rounded-md border border-zinc-700 bg-zinc-950 px-2 text-xs text-zinc-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-cyan-400 disabled:cursor-not-allowed disabled:opacity-50"
          value={typeof control.value === "string" || typeof control.value === "number" ? String(control.value) : ""}
          disabled={disabled}
          aria-busy={state.status === "pending"}
          aria-label={`${surface.title}: ${control.label}`}
          title={control.disabledReason}
          onChange={(event) => onInvoke(control, event.target.value)}
        >
          <option value="" disabled>Select</option>
          {options.map((option) => (
            <option key={option.value} value={option.value} disabled={option.disabled} title={option.disabledReason}>
              {option.label}{option.disabledReason ? ` — ${option.disabledReason}` : ""}
            </option>
          ))}
        </select>
      </label>
    );
  }

  const toggled = control.type === "toggle_button" && Boolean(control.value);
  return (
    <button
      type="button"
      className="inline-flex min-h-9 items-center gap-1.5 rounded-md border border-zinc-700/80 px-2.5 text-xs font-medium text-zinc-100 hover:bg-zinc-800/70 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-cyan-400 disabled:cursor-not-allowed disabled:opacity-50"
      disabled={disabled}
      aria-busy={state.status === "pending"}
      aria-pressed={control.type === "toggle_button" ? toggled : undefined}
      title={control.disabledReason}
      onClick={() => onInvoke(control, control.type === "toggle_button" ? !toggled : control.value)}
    >
      {state.status === "pending" ? <Loader2 size={14} className="animate-spin" aria-hidden="true" /> : iconFor(control.icon ?? (toggled ? "pause" : undefined), "h-3.5 w-3.5")}
      {control.label}
    </button>
  );
}

function StatusSurfaceCard({
  surface,
  modelOptions,
  providerOptions,
  thinkingOptions,
  onAction,
}: {
  surface: ResolvedStatusSurface;
  modelOptions: StatusSurfaceOption[];
  providerOptions: StatusSurfaceOption[];
  thinkingOptions: StatusSurfaceOption[];
  onAction?: StatusSurfaceHostProps["onAction"];
}) {
  const [detailsExpanded, setDetailsExpanded] = useState(false);
  const [actionStates, setActionStates] = useState<Record<string, ActionState>>({});
  const now = useElapsedClock(Boolean(surface.startedAt));
  const elapsed = formatElapsed(surface.startedAt, now);
  const progressPercent = surface.progress
    ? Math.min(100, Math.max(0, (surface.progress.current / surface.progress.total) * 100))
    : null;

  const invoke = async (control: ResolvedStatusSurfaceControl, value?: string | boolean | number | null) => {
    if (!control.actionId || !onAction) return;
    setActionStates((current) => ({ ...current, [control.id]: { status: "pending" } }));
    try {
      await onAction({
        surfaceId: surface.id,
        controlId: control.id,
        actionId: control.actionId,
        value,
        dataSourceId: surface.dataSourceId,
        sourceRevision: surface.sourceRevision,
      });
      setActionStates((current) => ({ ...current, [control.id]: { status: "success" } }));
    } catch (reason) {
      setActionStates((current) => ({ ...current, [control.id]: { status: "error", message: actionErrorMessage(reason) } }));
    }
  };
  const actionMessages = Object.entries(actionStates).filter(([, value]) => value.status === "error" || value.status === "success");

  return (
    <article
      className={`grid gap-2 rounded-xl border px-3 py-2 shadow-lg shadow-black/10 ${SEVERITY_STYLES[surface.severity]}`}
      data-status-surface-id={surface.id}
      data-status-surface-slot={surface.slot}
      data-status-surface-unsupported={surface.unsupported ? "true" : "false"}
      aria-labelledby={`status-surface-title-${surface.id}`}
    >
      <div className="flex min-w-0 flex-wrap items-start gap-2">
        <span className="mt-0.5 flex h-7 w-7 flex-none items-center justify-center rounded-full bg-black/20">
          {iconFor(surface.icon ?? (surface.unsupported ? "warning" : undefined))}
        </span>
        <div className="min-w-0 flex-1">
          <div className="flex min-w-0 flex-wrap items-center gap-2">
            <h3 id={`status-surface-title-${surface.id}`} className="truncate text-sm font-semibold">{surface.title}</h3>
            {surface.status && <span className="rounded-full border border-current/20 px-2 py-0.5 text-[10px] font-medium">{surface.status}</span>}
            {surface.count !== undefined && <span className="rounded-full bg-black/20 px-1.5 py-0.5 text-[10px]" aria-label={`${surface.count} items`}>{surface.count}</span>}
            {elapsed && <span className="inline-flex items-center gap-1 text-[11px] tabular-nums opacity-75"><Clock3 size={12} aria-hidden="true" />{elapsed}</span>}
          </div>
          {surface.summary && <p className="mt-0.5 line-clamp-2 text-xs leading-5 opacity-80">{surface.summary}</p>}
        </div>
        {surface.controls.length > 0 && (
          <div className="flex max-w-full flex-wrap items-end gap-1.5 max-[640px]:w-full">
            {surface.controls.map((control) => (
              <StatusControl
                key={control.id}
                surface={surface}
                control={control}
                state={actionStates[control.id] ?? { status: "idle" }}
                modelOptions={modelOptions}
                providerOptions={providerOptions}
                thinkingOptions={thinkingOptions}
                onInvoke={(target, value) => void invoke(target, value)}
                onToggleDetails={() => setDetailsExpanded((current) => !current)}
                detailsExpanded={detailsExpanded}
                actionAvailable={Boolean(onAction)}
              />
            ))}
          </div>
        )}
      </div>

      {surface.progress && progressPercent !== null && (
        <div className="grid grid-cols-[1fr_auto] items-center gap-2 text-[10px]">
          <div
            className="h-1.5 overflow-hidden rounded-full bg-black/25"
            role="progressbar"
            aria-label={surface.progress.label ?? `${surface.title} progress`}
            aria-valuemin={0}
            aria-valuemax={surface.progress.total}
            aria-valuenow={surface.progress.current}
          >
            <div className="h-full rounded-full bg-current transition-[width]" style={{ width: `${progressPercent}%` }} />
          </div>
          <span className="tabular-nums opacity-75">{surface.progress.current}/{surface.progress.total}</span>
        </div>
      )}

      {detailsExpanded && surface.details.length > 0 && (
        <dl id={`status-surface-details-${surface.id}`} className="grid gap-1 border-t border-current/10 pt-2 text-xs">
          {surface.details.map((detail, index) => (
            <div key={`${detail.label ?? "detail"}-${index}`} className="grid grid-cols-[minmax(0,8rem)_1fr] gap-2">
              <dt className="truncate font-medium opacity-60">{detail.label ?? "Detail"}</dt>
              <dd className="min-w-0 break-words">{detail.value}</dd>
            </div>
          ))}
        </dl>
      )}

      {actionMessages.length > 0 && (
        <div aria-live="polite" className="grid gap-1 text-[11px]">
          {actionMessages.map(([controlId, state]) => (
            <p key={controlId} role={state.status === "error" ? "alert" : "status"} className={state.status === "error" ? "text-rose-200" : "text-emerald-200"}>
              {state.status === "error" ? state.message : "Backend accepted the action; refreshing authoritative state."}
            </p>
          ))}
        </div>
      )}

      {surface.diagnostics.length > 0 && (
        <p className="text-[10px] opacity-70" data-status-surface-diagnostic={surface.diagnostics[0].code}>
          {surface.diagnostics[0].code} · {surface.templateId ?? "unknown template"} · {surface.trustLevel ?? "unknown trust"}
        </p>
      )}
    </article>
  );
}

export function StatusSurfaceHost({
  surfaces,
  slot,
  modelOptions = [],
  providerOptions = [],
  thinkingOptions = [],
  maxVisible = 3,
  onAction,
}: StatusSurfaceHostProps) {
  const [overflowExpanded, setOverflowExpanded] = useState(false);
  const matching = useMemo(
    () => surfaces
      .filter((surface) => surface.slot === slot)
      .sort((left, right) => (
        right.priority - left.priority
        || left.order - right.order
        || left.id.localeCompare(right.id)
      )),
    [slot, surfaces],
  );
  if (matching.length === 0) return null;
  const limit = Math.max(1, maxVisible);
  const visible = overflowExpanded ? matching : matching.slice(0, limit);
  const overflow = matching.length - visible.length;

  return (
    <section
      className="mx-2 mt-1 grid min-w-0 gap-1.5 max-[640px]:mx-1.5"
      aria-label={`Active status surfaces: ${slot.replace(/_/g, " ")}`}
      data-status-surface-host={slot}
    >
      {visible.map((surface) => (
        <StatusSurfaceCard
          key={surface.id}
          surface={surface}
          modelOptions={modelOptions}
          providerOptions={providerOptions}
          thinkingOptions={thinkingOptions}
          onAction={onAction}
        />
      ))}
      {(matching.length > limit || overflowExpanded) && (
        <button
          type="button"
          className="justify-self-end rounded-md px-2 py-1 text-[11px] text-zinc-400 hover:bg-zinc-800/70 hover:text-zinc-200 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-cyan-400"
          aria-expanded={overflowExpanded}
          onClick={() => setOverflowExpanded((current) => !current)}
        >
          {overflowExpanded ? "Show fewer status surfaces" : `Show ${overflow} more status surface${overflow === 1 ? "" : "s"}`}
        </button>
      )}
    </section>
  );
}
