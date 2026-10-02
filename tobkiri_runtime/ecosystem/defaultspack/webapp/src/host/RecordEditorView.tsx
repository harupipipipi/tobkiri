import { useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import type { FrontendCapabilityInvoker, FrontendCatalog } from "./frontendContracts";
import { viewOperationOutcome } from "./viewControlState";
import {
  readViewPath, viewOperationRequest,
  type RegisteredCatalogView, type ViewInputContext,
} from "./catalogViewRegistry";
import {
  beginRecordDraft, editorDisplayValue, filterEditorRecords, recordActionAvailable,
  recordDraftDirty, recordDraftUpdate, recordEditorRecords, recordOperationPayload, recordSavePayload,
  type RecordEditorAction, type RecordEditorDescriptor, type RecordEditorDraft,
  type RecordEditorGuardRegistration,
} from "./recordEditorState";

const buttonClass = "min-h-11 rounded border border-zinc-700 px-3 py-2 disabled:opacity-50";
const inputClass = "block min-h-11 w-full rounded border border-zinc-700 bg-transparent px-2 py-1";

/** Application-owned editor for validated catalog data and exact native actions. */
export function RecordEditorView({
  registered, catalog, capabilities, snapshot, onRefresh, onDirtyChange, context = {}, sourceReady = true,
}: {
  registered: RegisteredCatalogView; catalog: FrontendCatalog;
  capabilities: FrontendCapabilityInvoker; snapshot: unknown; onRefresh: () => void;
  onDirtyChange?: RecordEditorGuardRegistration; context?: ViewInputContext;
  sourceReady?: boolean;
}) {
  const descriptor = registered.view.record_editor;
  const [query, setQuery] = useState("");
  const [draft, setDraft] = useState<RecordEditorDraft | null>(null);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  const [feedback, setFeedback] = useState("");
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  const pendingRef = useRef(false);
  const activeRef = useRef(true);
  const confirmationRef = useRef<{
    id: string; update: Record<string, unknown>; values: string;
  } | null>(null);
  const stateRef = useRef({ draft, pending });
  stateRef.current = { draft, pending };
  const dialogRef = useRef<HTMLDialogElement>(null);
  const dialogTitleId = useId();
  const rows = useMemo(() => descriptor ? recordEditorRecords(snapshot, descriptor) : null,
    [snapshot, descriptor]);
  const filtered = useMemo(() => descriptor && rows ? filterEditorRecords(rows, descriptor, query) : [],
    [rows, descriptor, query]);
  const dirty = recordDraftDirty(draft);
  const ownerId = JSON.stringify(registered.reference);
  const canLeave = useCallback(() => {
    const current = stateRef.current;
    if (current.pending || pendingRef.current) return false;
    if (!recordDraftDirty(current.draft)) return true;
    if (typeof window === "undefined" || !window.confirm("Discard unsaved changes?")) return false;
    return true;
  }, []);
  useEffect(() => {
    activeRef.current = true;
    return () => { activeRef.current = false; };
  }, []);
  useEffect(() => {
    onDirtyChange?.(ownerId, canLeave);
    return () => onDirtyChange?.(ownerId, null);
  }, [ownerId, onDirtyChange, canLeave]);
  useEffect(() => {
    const beforeUnload = (event: BeforeUnloadEvent) => {
      if (!pendingRef.current && !recordDraftDirty(stateRef.current.draft)) return;
      event.preventDefault(); event.returnValue = "";
    };
    window.addEventListener("beforeunload", beforeUnload);
    return () => window.removeEventListener("beforeunload", beforeUnload);
  }, []);
  useEffect(() => {
    if (draft && dialogRef.current && !dialogRef.current.open) dialogRef.current.showModal();
  }, [draft]);
  useEffect(() => {
    const expected = confirmationRef.current;
    if (!expected || !descriptor || !rows) return;
    const current = rows.find((record) => readViewPath(record, descriptor.id_path) === expected.id);
    if (current && Object.entries(expected.update).every(([key, value]) =>
      JSON.stringify(readViewPath(current, key)) === JSON.stringify(value))) {
      confirmationRef.current = null;
      setDraft((previous) => previous?.id === expected.id
        && JSON.stringify(previous.values) === expected.values ? null : previous);
      setFeedback("The authoritative record now contains the saved changes.");
    }
  }, [snapshot, rows, descriptor]);

  if (!descriptor || !rows) return <p role="status">The record data source is unavailable. Previous drafts are retained.</p>;

  const close = () => { if (canLeave()) { setDraft(null); setFieldErrors({}); } };
  const open = (record: Record<string, unknown>) => {
    if (!canLeave()) return;
    setDraft(beginRecordDraft(record, descriptor, snapshot));
    setFieldErrors({}); setError(""); setFeedback("");
  };
  const invoke = async (action: RecordEditorAction | RecordEditorDescriptor["save"],
                        payload: Record<string, unknown> | null, save: boolean) => {
    if (pendingRef.current || !sourceReady || !payload) return;
    const request = viewOperationRequest(catalog, registered, action.operation, payload);
    if (!request) { setError("This operation is unavailable in the current Profile."); return; }
    pendingRef.current = true; setPending(true); setError(""); setFeedback("");
    try {
      const result = await capabilities.invokeAction(request);
      if (!activeRef.current) return;
      const outcome = viewOperationOutcome(result);
      if (outcome === "approval") {
        setFeedback("Waiting for approval in the trusted Tobkiri approval surface. The draft is retained.");
      } else {
        if (outcome === "failed") throw new Error("operation_failed");
        setFeedback("The operation returned. Refreshing the authoritative state.");
        if (save && stateRef.current.draft) {
          const current = stateRef.current.draft;
          const { update } = recordDraftUpdate(current, descriptor);
          if (update) confirmationRef.current = {
            id: current.id, update, values: JSON.stringify(current.values),
          };
        }
      }
      onRefresh();
    } catch {
      if (activeRef.current) setError("The operation failed or the record changed. Your draft is retained. Refresh and retry, or discard changes explicitly.");
    } finally {
      pendingRef.current = false;
      if (activeRef.current) setPending(false);
    }
  };
  const save = () => {
    if (!draft || pendingRef.current) return;
    const { payload, errors } = recordSavePayload(descriptor, snapshot, draft, context);
    setFieldErrors(errors);
    if (!payload) { setError("The draft could not be saved. Check the fields and current operation availability."); return; }
    void invoke(descriptor.save, payload, true);
  };
  const rowAction = (action: RecordEditorAction, record: Record<string, unknown>) => {
    if (!canLeave()) return;
    void invoke(action, recordOperationPayload(action, snapshot, record, context), false);
  };

  return <div data-tobkiri-record-editor className="flex min-w-0 flex-col gap-3">
    <label className="text-sm">Search records
      <input type="search" maxLength={256} value={query} onChange={(event) => setQuery(event.target.value)}
        className={inputClass} /></label>
    <p role="status">{filtered.length} of {rows.length} records</p>
    {error && <p role="alert" className="text-sm">{error}</p>}
    {feedback && <p role="status" className="text-sm">{feedback}</p>}
    <div className="overflow-x-auto">
      <table className="w-full text-left text-sm">
        <thead><tr>{(descriptor.columns ?? []).map((column, index) => <th key={index} className="p-2">{column.label}</th>)}
          <th className="p-2">Actions</th></tr></thead>
        <tbody>{filtered.map((record) => {
          const id = String(readViewPath(record, descriptor.id_path));
          const title = editorDisplayValue(readViewPath(record, descriptor.title_path));
          return <tr key={id}>
            {(descriptor.columns ?? []).map((column, index) => <td key={index} className="max-w-80 break-words p-2">
              {editorDisplayValue(readViewPath(record, column.path), column.kind)}
            </td>)}
            <td className="flex flex-wrap gap-2 p-2"><button type="button" className={buttonClass}
              aria-label={`Edit ${title}`} disabled={pending} onClick={() => open(record)}>Edit</button>
              {(descriptor.actions ?? []).map((action) => {
                if (!recordActionAvailable(action, record)) return null;
                const payload = recordOperationPayload(action, snapshot, record, context);
                const available = payload && viewOperationRequest(catalog, registered, action.operation, payload);
                return <button type="button" key={action.id} className={buttonClass}
                  disabled={pending || !sourceReady || !available} aria-label={`${action.label} ${title}`}
                  onClick={() => rowAction(action, record)}>{action.label}</button>;
              })}</td>
          </tr>;
        })}</tbody>
      </table>
      {filtered.length === 0 && <p role="status">No matching records.</p>}
    </div>
    {draft && <dialog ref={dialogRef} aria-labelledby={dialogTitleId}
      onCancel={(event) => { event.preventDefault(); close(); }}
      className="max-h-[90vh] w-[min(42rem,95vw)] overflow-auto rounded-lg border border-zinc-700 bg-zinc-950 p-4 text-zinc-100">
      <h3 id={dialogTitleId}>Edit {editorDisplayValue(readViewPath(draft.record, descriptor.title_path))}</h3>
      <form onSubmit={(event) => { event.preventDefault(); save(); }} className="mt-3 flex flex-col gap-3">
        {descriptor.fields.map((field) => <label key={field.id} className="text-sm">{field.label}
          {field.kind === "multiline" || field.kind === "json" ? <textarea className={inputClass} rows={5}
            maxLength={16384} readOnly={field.read_only} disabled={pending} value={draft.values[field.id] ?? ""}
            aria-invalid={Boolean(fieldErrors[field.id])} onChange={(event) => setDraft((current) => current ? {
              ...current, values: { ...current.values, [field.id]: event.target.value },
            } : current)} /> : <input className={inputClass}
            type={field.kind === "integer" ? "number" : field.kind === "datetime" ? "datetime-local" : "text"}
            step={field.kind === "datetime" ? 1 : field.kind === "integer" ? 1 : undefined}
            min={field.kind === "integer" ? field.min : undefined} max={field.kind === "integer" ? field.max : undefined}
            maxLength={16384} readOnly={field.read_only} disabled={pending} value={draft.values[field.id] ?? ""}
            aria-invalid={Boolean(fieldErrors[field.id])} onChange={(event) => setDraft((current) => current ? {
              ...current, values: { ...current.values, [field.id]: event.target.value },
            } : current)} />}
          {fieldErrors[field.id] && <span role="alert">{fieldErrors[field.id]}</span>}
        </label>)}
        {fieldErrors._form && <p role="alert">{fieldErrors._form}</p>}
        <div className="flex flex-wrap gap-2"><button type="submit" className={buttonClass} disabled={pending || !sourceReady || !dirty}>
          {pending ? "Working…" : "Save changes"}</button>
          <button type="button" className={buttonClass} disabled={pending} onClick={close}>Close</button>
          <button type="button" className={buttonClass} disabled={pending || !dirty} onClick={() => {
            setDraft(beginRecordDraft(draft.record, descriptor, draft.source)); setFieldErrors({});
          }}>Discard changes</button>
        </div>
      </form>
    </dialog>}
  </div>;
}
