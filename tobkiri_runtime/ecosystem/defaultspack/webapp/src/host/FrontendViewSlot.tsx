import {
  Component, useCallback, useEffect, useMemo, useRef, useState,
  type ReactNode,
} from "react";
import type { FrontendCapabilityInvoker, FrontendCatalog } from "./frontendContracts";
import { RecordEditorView } from "./RecordEditorView";
import { freshTextDraft, refreshTextDraft, textDraftDirty, viewOperationOutcome } from "./viewControlState";
import {
  choicePayload, controlPayload, matchesViewReference, readViewPath,
  viewChoices, viewOperationRequest, viewReadRequest, viewsForSlot, requestContextInput, viewContextKey,
  type CatalogViewReference, type RegisteredCatalogView,
  type ViewControl, type ViewSlot, type ViewInputContext, type ViewNavigationGuardChange,
} from "./catalogViewRegistry";

/** Render approved slot declarations through code shipped by the Application. */
export function FrontendViewSlot({
  catalog, slot, activePlanHash, capabilities, contributionId, reference, context = {},
  onNavigationGuardChange,
}: {
  catalog: FrontendCatalog; slot: ViewSlot; activePlanHash: string;
  capabilities: FrontendCapabilityInvoker; contributionId?: string;
  reference?: CatalogViewReference;
  context?: ViewInputContext;
  onNavigationGuardChange?: ViewNavigationGuardChange;
}) {
  const views = useMemo(() => viewsForSlot(catalog, slot, activePlanHash)
    .filter((registered) => !contributionId || registered.item.contribution_id === contributionId)
    .filter((registered) => !reference || matchesViewReference(registered, reference)),
  [catalog, slot, activePlanHash, contributionId, reference]);
  if (contributionId && views.length !== 1) return <UnavailableView />;
  return <div data-tobkiri-view-slot={slot} className="flex min-w-0 flex-col gap-2">
    {views.map((registered) => <ViewBoundary
      key={JSON.stringify([registered.reference, viewContextKey(registered, context)])}
      fallback={<UnavailableView />}>
      <CatalogViewHost registered={registered} catalog={catalog} capabilities={capabilities}
        context={context} onNavigationGuardChange={onNavigationGuardChange} />
    </ViewBoundary>)}
  </div>;
}

function UnavailableView() {
  return <p role="status" data-tobkiri-view-unavailable>
    This Pack view is unavailable in the current Profile.
  </p>;
}

class ViewBoundary extends Component<
  { children: ReactNode; fallback: ReactNode }, { failed: boolean }
> {
  state = { failed: false };
  static getDerivedStateFromError() { return { failed: true }; }
  render() { return this.state.failed ? this.props.fallback : this.props.children; }
}

function safeText(value: unknown): string {
  if (typeof value === "string") return value.slice(0, 4096);
  if (typeof value === "number" && Number.isFinite(value)) return String(value);
  if (typeof value === "boolean") return value ? "Yes" : "No";
  return "Unavailable";
}

function CatalogViewHost({
  registered, catalog, capabilities, context, onNavigationGuardChange,
}: {
  registered: RegisteredCatalogView; catalog: FrontendCatalog;
  capabilities: FrontendCapabilityInvoker;
  context: ViewInputContext;
  onNavigationGuardChange?: ViewNavigationGuardChange;
}) {
  const [snapshot, setSnapshot] = useState<unknown>(null);
  const [loading, setLoading] = useState(false);
  const [sourceError, setSourceError] = useState(false);
  const [refreshIndex, setRefreshIndex] = useState(0);
  const source = registered.view.data_source;
  const sourceInput = source ? requestContextInput(source, context) : null;
  const sourceRequest = source && sourceInput
    ? viewReadRequest(catalog, registered, source, sourceInput) : null;
  const sourceAvailable = !source || sourceRequest !== null;
  useEffect(() => {
    if (!source || !sourceRequest) return undefined;
    let active = true;
    setLoading(true);
    setSourceError(false);
    void capabilities.readDataSource(sourceRequest).then((result) => {
      if (active) setSnapshot(result);
    }).catch(() => {
      if (active) setSourceError(true);
    }).finally(() => {
      if (active) setLoading(false);
    });
    return () => { active = false; };
    // Source identity and constants are bound by the descriptor/capture key.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [registered, capabilities, refreshIndex]);
  const refresh = useCallback(() => setRefreshIndex((value) => value + 1), []);
  const sourceReady = sourceAvailable && !loading && !sourceError && (!source || snapshot !== null);
  return <section aria-label={registered.item.accessibility.name}
    aria-live={registered.item.accessibility.live === "off" ? undefined : registered.item.accessibility.live}
    data-contribution-id={registered.item.contribution_id}
    data-tobkiri-view-renderer={registered.view.renderer}
    className="min-w-0 rounded-lg border border-zinc-700/60 p-3">
    <h2 className="break-words text-sm font-medium">{registered.view.title ?? registered.item.label}</h2>
    {registered.view.body && <p className="break-words whitespace-pre-wrap text-sm">{registered.view.body}</p>}
    {!sourceAvailable && <UnavailableView />}
    {loading && <p role="status">Loading…</p>}
    {sourceError && <p role="alert">The data source could not be read. Previous state is retained.</p>}
    {source && <button type="button" disabled={loading || !sourceAvailable} onClick={refresh}
      className="min-h-11 rounded px-3 py-2">Refresh</button>}
    <dl className="grid min-w-0 gap-2">
      {(registered.view.fields ?? []).map((field, index) => {
        const value = readViewPath(snapshot, field.path);
        const total = readViewPath(snapshot, field.total_path);
        const progress = field.kind === "progress" && typeof value === "number"
          && typeof total === "number" && Number.isFinite(value)
          && Number.isFinite(total) && total > 0 && value >= 0 && value <= total;
        return <div key={index} className="min-w-0">
          <dt className="text-xs text-zinc-400">{field.label}</dt>
          <dd className="break-words whitespace-pre-wrap text-sm">
            {progress ? <><progress aria-label={field.label} value={value} max={total} />
              <span>{value} / {total}</span></> : safeText(value)}
          </dd>
        </div>;
      })}
    </dl>
    <div className="mt-2 flex min-w-0 flex-wrap gap-2">
      {registered.view.renderer === "record_editor" && <RecordEditorView
        registered={registered} catalog={catalog} capabilities={capabilities} snapshot={snapshot}
        onRefresh={refresh} context={context} sourceReady={sourceReady}
        onDirtyChange={onNavigationGuardChange} />}
      {registered.view.renderer === "conversation_thread" && <UnavailableView />}
      {(registered.view.controls ?? []).map((control) => <CatalogControl
        key={control.id} control={control} snapshot={snapshot}
        registered={registered} catalog={catalog} capabilities={capabilities}
        context={context}
        onNavigationGuardChange={onNavigationGuardChange}
        sourceReady={sourceReady}
        onRefresh={refresh} />)}
    </div>
  </section>;
}

function CatalogControl({
  control, snapshot, registered, catalog, capabilities, sourceReady, onRefresh, context,
  onNavigationGuardChange,
}: {
  control: ViewControl; snapshot: unknown; registered: RegisteredCatalogView;
  catalog: FrontendCatalog; capabilities: FrontendCapabilityInvoker;
  sourceReady: boolean; onRefresh: () => void;
  context: ViewInputContext;
  onNavigationGuardChange?: ViewNavigationGuardChange;
}) {
  const authoritative = readViewPath(snapshot, control.value_path);
  const [textDraft, setTextDraft] = useState(() => freshTextDraft(authoritative));
  const [query, setQuery] = useState("");
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState(false);
  const [feedback, setFeedback] = useState<string | null>(null);
  const pending = useRef(false);
  const active = useRef(true);
  const currentDraft = useRef(textDraft);
  currentDraft.current = textDraft;
  useEffect(() => { active.current = true; return () => { active.current = false; }; }, []);
  useEffect(() => {
    const beforeUnload = (event: BeforeUnloadEvent) => {
      if (!pending.current && (control.kind !== "text" || !textDraftDirty(currentDraft.current))) return;
      event.preventDefault(); event.returnValue = "";
    };
    window.addEventListener("beforeunload", beforeUnload);
    return () => window.removeEventListener("beforeunload", beforeUnload);
  }, [control.kind]);
  useEffect(() => {
    setTextDraft((current) => refreshTextDraft(current, authoritative));
  }, [authoritative, textDraft.awaiting]);
  const ownerId = JSON.stringify([registered.reference, control.id, viewContextKey(registered, context)]);
  useEffect(() => {
    onNavigationGuardChange?.(ownerId, () => {
      if (pending.current) return false;
      return control.kind !== "text" || !textDraftDirty(currentDraft.current)
        || window.confirm("Discard unsaved changes to " + control.label + "?");
    });
    return () => onNavigationGuardChange?.(ownerId, null);
  }, [ownerId, control.kind, control.label, onNavigationGuardChange]);
  const base = controlPayload(control, snapshot, control.kind === "toggle"
    ? authoritative !== true : control.kind === "text" ? textDraft.value : null, context);
  const available = base !== null && viewOperationRequest(catalog, registered, control.operation, base) !== null;
  const disabled = !sourceReady || !available || busy;
  const invoke = async (payload: Record<string, unknown> | null) => {
    if (pending.current || disabled || !payload) return;
    const request = viewOperationRequest(catalog, registered, control.operation, payload);
    if (!request) return;
    pending.current = true;
    setBusy(true);
    setFailed(false);
    setFeedback(null);
    try {
      const result = await capabilities.invokeAction(request);
      if (active.current) {
        const outcome = viewOperationOutcome(result);
        if (outcome === "failed") throw new Error("operation_failed");
        if (control.kind === "text" && outcome === "returned") {
          const submitted = textDraft.value;
          setTextDraft((current) => ({ ...current, awaiting: submitted }));
        }
        setFeedback(outcome === "approval"
          ? "Waiting for approval in the trusted Tobkiri approval surface."
          : "The operation returned. Refreshing the authoritative state.");
        onRefresh();
      }
    } catch {
      if (active.current) {
        setFailed(true);
      }
    } finally {
      pending.current = false;
      if (active.current) setBusy(false);
    }
  };
  const choices = viewChoices(control, snapshot);
  const selected = control.multiple
    ? Array.isArray(authoritative) ? authoritative.filter((item): item is string => typeof item === "string") : []
    : typeof authoritative === "string" ? authoritative : "";
  return <div className="flex min-w-0 flex-col gap-1" data-view-control={control.id}>
    {control.kind === "text" && <label className="text-sm">{control.label}
      <input value={textDraft.value} maxLength={16384} disabled={busy}
        onChange={(event) => setTextDraft((current) => ({ ...current, value: event.target.value, awaiting: null }))}
        className="block min-h-11 w-full rounded border border-zinc-700 bg-transparent px-2" /></label>}
    {control.kind === "choice" ? <>
      <label className="text-sm">Search {control.label}
        <input value={query} disabled={disabled} maxLength={256}
          onChange={(event) => setQuery(event.target.value)}
          className="block min-h-11 w-full rounded border border-zinc-700 bg-transparent px-2" /></label>
      <label className="text-sm">{control.label}
        <select multiple={control.multiple} value={selected} disabled={disabled}
          onChange={(event) => void invoke(choicePayload(control, snapshot,
            Array.from(event.target.selectedOptions).map((option) => option.value), context))}
          className="block min-h-11 w-full rounded border border-zinc-700 bg-zinc-950 px-2">
          {!control.multiple && <option value="" disabled>Choose an item</option>}
          {choices.filter((choice) => choice.label.toLocaleLowerCase().includes(query.toLocaleLowerCase()))
            .map((choice) => <option key={choice.id} value={choice.id} disabled={choice.disabled}>
              {choice.label}{choice.disabled ? " (unavailable)" : ""}
            </option>)}
        </select>
      </label>
      {choices.length === 0 && <p role="status">No items are available.</p>}
    </> : <button type="button" disabled={disabled}
      aria-pressed={control.kind === "toggle" ? authoritative === true : undefined}
      onClick={() => void invoke(base)} className="min-h-11 rounded border border-zinc-700 px-3 py-2">
      {busy ? "Working…" : control.kind === "text" ? "Save " + control.label : control.label}
    </button>}
    {control.kind === "text" && textDraftDirty(textDraft) && <button type="button" disabled={busy}
      onClick={() => setTextDraft(freshTextDraft(authoritative))}
      className="min-h-11 rounded border border-zinc-700 px-3 py-2">Reload saved value</button>}
    {!available && <p role="status" className="text-xs">Operation unavailable</p>}
    {failed && <p role="alert" className="text-xs">The operation failed. Previous state is retained; try again.</p>}
    {feedback && <p role="status" className="text-xs">{feedback}</p>}
  </div>;
}
