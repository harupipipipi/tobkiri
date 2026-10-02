import { useEffect, useRef, useState } from "react";
import type { FrontendCapabilityInvoker, FrontendCatalog } from "./frontendContracts";
import {
  matchesViewReference, viewContextKey, viewOperationRequest, viewReference,
  type RegisteredCatalogView, type ViewInputContext, type ViewNavigationGuardChange,
} from "./catalogViewRegistry";
import { resolveSurfaceRenderer } from "./surfaceRendererCapture";
import {
  normalizeSurfaceIntent, parseSurfaceOutcome, parseSurfaceResource, surfaceReadPath,
  type SurfaceIntentDefinition, type SurfaceNode, type SurfaceRequest, type SurfaceResource,
  type SurfaceTemplate,
} from "./surfaceTemplateContract";
import { surfaceCollection, surfaceDisplayText, surfaceRequestPayload } from "./surfaceTemplateState";
import { dispatchSurfaceRequest } from "./surfaceTemplateDispatch";

type Props = {
  registered: RegisteredCatalogView; catalog: FrontendCatalog; capabilities: FrontendCapabilityInvoker;
  snapshot: unknown; sourceReady: boolean; context?: ViewInputContext; onRefresh: () => void;
  onDirtyChange?: ViewNavigationGuardChange;
};

/** Two shipped renderers interpret the same neutral nodes and exact intents. */
export function SurfaceTemplatesView(props: Props) {
  const template = props.registered.view.surface_template;
  const renderer = template ? resolveSurfaceRenderer(props.catalog, template) : null;
  if (!template || !renderer || !matchesViewReference({ ...props.registered,
    reference: viewReference(props.catalog, props.registered.item) }, props.registered.reference)) {
    return <p role="status">The selected Surface renderer is unavailable.</p>;
  }
  const compact = renderer.descriptor.implementation === "semantic_compact";
  return <div data-surface-template={template.template_id}
    data-surface-renderer={renderer.descriptor.implementation} data-surface-motion="static"
    className={compact ? "grid min-w-0 gap-1" : "flex min-w-0 flex-col gap-4"}>
    {template.nodes.map((node) => <SurfaceNodeView key={node.id} {...props} template={template} node={node} compact={compact} />)}
  </div>;
}

function SurfaceNodeView({
  registered, catalog, capabilities, snapshot, sourceReady, context = {}, onRefresh,
  onDirtyChange, template, node, compact,
}: Props & { template: SurfaceTemplate; node: SurfaceNode; compact: boolean }) {
  const [values, setValues] = useState<Record<string, unknown>>({});
  const [resource, setResource] = useState<SurfaceResource | null>(null);
  const [feedback, setFeedback] = useState<string | null>(null);
  const [failure, setFailure] = useState(false);
  const [busy, setBusy] = useState(false);
  const pending = useRef(false);
  const active = useRef(true);
  const draft = useRef(values);
  draft.current = values;
  const ownerId = JSON.stringify([registered.reference, node.id, viewContextKey(registered, context)]);
  const dirty = useRef(false);
  dirty.current = Object.keys(values).length > 0 || resource !== null;
  useEffect(() => {
    active.current = true;
    onDirtyChange?.(ownerId, () => !pending.current
      && (!dirty.current || window.confirm(`Discard unsent changes to ${node.label}?`)));
    const beforeUnload = (event: BeforeUnloadEvent) => {
      if (!pending.current && !dirty.current) return;
      event.preventDefault(); event.returnValue = "";
    };
    window.addEventListener("beforeunload", beforeUnload);
    return () => { active.current = false; onDirtyChange?.(ownerId, null); window.removeEventListener("beforeunload", beforeUnload); };
  }, [ownerId, node.label, onDirtyChange]);
  const current = () => resolveSurfaceRenderer(catalog, template) !== null
    && matchesViewReference({ ...registered, reference: viewReference(catalog, registered.item) }, registered.reference);
  const requestFor = (request: SurfaceRequest, additions: Record<string, unknown> = {}) => {
    const payload = surfaceRequestPayload(request, snapshot, context, additions);
    return current() && payload ? viewOperationRequest(catalog, registered, request.operation, payload) : null;
  };
  const execute = async (request: SurfaceRequest, additions: Record<string, unknown>, accept: (value: unknown) => void) => {
    const invocation = requestFor(request, additions);
    if (!invocation || !sourceReady || pending.current) return;
    pending.current = true; setBusy(true); setFailure(false); setFeedback(null);
    try {
      const outcome = await dispatchSurfaceRequest(catalog, registered, capabilities, request, snapshot,
        context, additions, () => active.current && current());
      if (!outcome) return;
      if (outcome.kind === "approval") setFeedback("Waiting for approval in the trusted Tobkiri approval surface.");
      else accept(outcome.result);
    } catch {
      if (active.current) { setFailure(true); setFeedback(null); }
    } finally { pending.current = false; if (active.current) setBusy(false); }
  };
  const submit = (definition: SurfaceIntentDefinition) => {
    const data = node.pattern === "resource_input" ? { resource } : values;
    const intent = normalizeSurfaceIntent(template, node.id, definition.id, data);
    if (!intent) { setFailure(true); return; }
    const submitted = JSON.stringify(values);
    void execute(definition.request, { surface_intent: intent }, (result) => {
      const outcome = parseSurfaceOutcome(surfaceReadPath(result, definition.outcome_path), intent);
      if (!outcome) throw new Error("surface_outcome_unconfirmed");
      setFeedback(outcome.message ?? (outcome.status === "accepted" ? "The declared intent was accepted."
        : outcome.status === "pending" ? "The declared intent is pending." : "The declared intent was rejected."));
      if (outcome.status === "accepted") {
        if (JSON.stringify(draft.current) === submitted) { setValues({}); setResource(null); }
        onRefresh();
      }
    });
  };
  const acquire = () => {
    if (!node.resource) return;
    void execute(node.resource.acquire, {}, (result) => {
      const selected = parseSurfaceResource(result, node.resource!.kind, "selected");
      if (!selected) throw new Error("surface_resource_unconfirmed");
      setResource(selected); setFeedback("Resource selected. Exchange it for this declared consumer before submitting.");
    });
  };
  const exchange = () => {
    if (!node.resource || !resource || !parseSurfaceResource(resource, node.resource.kind, "selected")) return;
    void execute(node.resource.exchange, { selection_id: resource.selection_id }, (result) => {
      const exchanged = parseSurfaceResource(result, node.resource!.kind, "exchanged");
      if (!exchanged) throw new Error("surface_resource_exchange_unconfirmed");
      setResource(exchanged); setFeedback("Resource exchanged for the declared consumer.");
    });
  };
  const collection = node.pattern === "collection" ? surfaceCollection(node, snapshot) : null;
  const selected = Array.isArray(values.selected) ? values.selected.filter((item): item is string => typeof item === "string") : [];
  const fields = values.fields && typeof values.fields === "object" ? values.fields as Record<string, unknown> : {};
  const setField = (id: string, value: unknown) => setValues((previous) => ({ fields: {
    ...(previous.fields as Record<string, unknown> ?? {}), [id]: value,
  } }));
  const value = surfaceReadPath(snapshot, node.value_path);
  const total = surfaceReadPath(snapshot, node.total_path);
  const progress = typeof value === "number" && Number.isFinite(value) && typeof total === "number"
    && Number.isFinite(total) && total > 0 && value >= 0 && value <= total;
  return <section aria-label={node.label} data-surface-pattern={node.pattern}
    className={compact ? "min-w-0 border-b border-zinc-700 py-1" : "min-w-0 rounded border border-zinc-700 p-3"}>
    <h3 className="font-medium">{node.label}</h3>
    {node.body && <p className="whitespace-pre-wrap break-words">{node.body}</p>}
    {node.value_path && node.pattern !== "progress" && <p className="whitespace-pre-wrap break-words">{surfaceDisplayText(value)}</p>}
    {node.pattern === "problem" && <p role="alert">{node.body ?? "A problem was reported by this Surface."}</p>}
    {node.pattern === "notice" && <p role="status">{node.body ?? "A notice was reported by this Surface."}</p>}
    {node.pattern === "progress" && (progress ? <><progress aria-label={node.label} value={value} max={total} />
      <span>{value} / {total}</span></> : <p role="status">Progress is unavailable.</p>)}
    {node.pattern === "choice" && <label>{node.label}<select multiple={node.multiple} value={node.multiple ? selected : selected[0] ?? ""}
      disabled={busy || !sourceReady} onChange={(event) => setValues({ selected: Array.from(event.target.selectedOptions).map((option) => option.value) })}>
      {!node.multiple && <option value="" disabled>Choose an item</option>}
      {node.choices?.map((choice) => <option key={choice.id} value={choice.id}>{choice.label}</option>)}
    </select></label>}
    {node.pattern === "form" && <fieldset disabled={busy || !sourceReady}>
      <legend>{node.label}</legend>
      {node.fields?.map((field) => <label key={field.id} className="block">{field.label}
        {field.type === "boolean" ? <input type="checkbox" checked={fields[field.id] === true}
          onChange={(event) => setField(field.id, event.target.checked)} />
          : <input type={field.type === "integer" ? "number" : "text"} required={field.required}
            min={field.min} max={field.max} step={field.type === "integer" ? 1 : undefined} maxLength={16384}
            value={typeof fields[field.id] === "string" || typeof fields[field.id] === "number" ? String(fields[field.id]) : ""}
            onChange={(event) => setField(field.id, field.type === "integer" ? event.target.value === "" ? null : Number(event.target.value) : event.target.value)} />}
      </label>)}
    </fieldset>}
    {node.pattern === "collection" && (collection ? <label>{node.label}<select value={String(values.selected_id ?? "")}
      disabled={busy || !sourceReady} onChange={(event) => setValues({ selected_id: event.target.value })}>
      <option value="" disabled>Choose an item</option>
      {collection.map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}
    </select></label> : <p role="status">Collection is unavailable.</p>)}
    {node.resource && <div>
      <button type="button" disabled={busy || !sourceReady || !requestFor(node.resource.acquire)} onClick={acquire}>Choose {node.resource.kind}</button>
      {!requestFor(node.resource.acquire) && <p role="status">Resource acquisition provider is unavailable.</p>}
      {resource && <p>{resource.display_name}</p>}
      <button type="button" disabled={busy || !sourceReady || !resource || !parseSurfaceResource(resource, node.resource.kind, "selected")
        || !requestFor(node.resource.exchange, { selection_id: resource.selection_id })} onClick={exchange}>Exchange selected resource</button>
    </div>}
    <div className="flex flex-wrap gap-2">{node.intents?.map((intent) => {
      const data = node.pattern === "resource_input" ? { resource } : values;
      const typed = normalizeSurfaceIntent(template, node.id, intent.id, data);
      const selectable = node.pattern !== "collection" || collection?.some((item) => item.id === values.selected_id);
      return <button key={intent.id} type="button" disabled={busy || !sourceReady || !typed || !selectable
        || !requestFor(intent.request, { surface_intent: typed })} onClick={() => submit(intent)}>{intent.label}</button>;
    })}</div>
    {busy && <p role="status">Working…</p>}
    {failure && <p role="alert">The operation or its typed outcome could not be confirmed. Unsent values are retained.</p>}
    {feedback && <p role="status">{feedback}</p>}
  </section>;
}
