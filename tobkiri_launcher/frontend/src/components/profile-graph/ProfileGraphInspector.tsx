import {Badge} from '@/src/components/ui/Badge';
import {Button} from '@/src/components/ui/Button';
import type {ApiProfileGraphDocument, ApiProfileGraphNode, ApiProfileGraphSelected} from '@/src/lib/apiTypes';
import {categoryForGraphNodeId, graphNodeKindLabel, type ProfileGraphCategory} from '@/src/lib/profileGraph';

interface ProfileGraphInspectorProps {
  document: ApiProfileGraphDocument;
  node: ApiProfileGraphNode | null;
  preview?: {
    selected?: ApiProfileGraphSelected;
    policy?: Record<string, unknown>;
    prompt_resolution?: Record<string, unknown>;
    api_route_policy?: Record<string, unknown>;
    tool_filter_result?: Array<Record<string, unknown>>;
  } | null;
  onRemoveSelection: (category: ProfileGraphCategory, ref: string) => void;
}

export function ProfileGraphInspector({
  document,
  node,
  preview,
  onRemoveSelection,
}: ProfileGraphInspectorProps) {
  if (!node) {
    return (
      <section className="rounded-2xl border border-border bg-bg-card p-4">
        <h2 className="text-sm font-semibold text-text-main">Inspector</h2>
        <p className="mt-2 text-sm text-text-muted">Select a node to inspect source metadata, runtime impact, and policy wiring.</p>
      </section>
    );
  }

  const category = categoryForGraphNodeId(node.id);
  const canRemove = Boolean(category && node.ref);
  const previewSelected = preview?.selected && category ? preview.selected[category] : undefined;
  const launchSurface = isLaunchSurfaceNode(node);

  return (
    <section className="rounded-2xl border border-border bg-bg-card p-4">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="text-xs uppercase tracking-[0.18em] text-text-muted">{graphNodeKindLabel(node)}</div>
          <h2 className="truncate text-lg font-semibold text-text-main">{node.label || node.ref || node.id}</h2>
          <div className="truncate text-xs text-text-muted">{node.id}</div>
        </div>
        {canRemove ? (
          <Button type="button" size="sm" variant="outline" onClick={() => onRemoveSelection(category!, node.ref)}>
            Remove
          </Button>
        ) : null}
      </div>

      <div className="mt-4 flex flex-wrap gap-2">
        <Badge variant="outline">{node.kind}</Badge>
        {category ? <Badge variant="secondary">{category}</Badge> : null}
        {launchSurface ? <Badge variant="default">launch surface</Badge> : null}
        {Array.isArray(previewSelected) && previewSelected.includes(node.ref) ? (
          <Badge variant="default">active in preview</Badge>
        ) : null}
      </div>

      {category === 'prompts' && preview?.prompt_resolution ? (
        <div className="mt-4 rounded-xl border border-fuchsia-400/30 bg-fuchsia-500/8 p-3 text-xs text-text-muted">
          <div className="font-medium text-text-main">Rule Resolution</div>
          <div className="mt-1">selected_prompt_id: {String(preview.prompt_resolution.selected_prompt_id ?? '--')}</div>
          <div>source_type: {String(preview.prompt_resolution.source_type ?? '--')}</div>
          <div className="break-all">source: {String(preview.prompt_resolution.source ?? '--')}</div>
        </div>
      ) : null}

      {category === 'api_routes' && preview?.api_route_policy ? (
        <div className="mt-4 rounded-xl border border-amber-400/30 bg-amber-500/8 p-3 text-xs text-text-muted">
          <div className="font-medium text-text-main">API Policy</div>
          <div>strict: {String(Boolean(preview.api_route_policy.enforce))}</div>
          <div>allowlist entries: {Array.isArray(preview.api_route_policy.allowlist) ? preview.api_route_policy.allowlist.length : 0}</div>
        </div>
      ) : null}

      {category === 'tools' && Array.isArray(preview?.tool_filter_result) ? (
        <div className="mt-4 rounded-xl border border-emerald-400/30 bg-emerald-500/8 p-3 text-xs text-text-muted">
          <div className="font-medium text-text-main">Tool Filter</div>
          <div>
            {preview.tool_filter_result
              .filter((entry) => String(entry.tool_name ?? '') === node.ref)
              .map((entry) => `${String(entry.status ?? '--')} / ${String(entry.reason_code ?? '--')}`)
              .join(', ') || '--'}
          </div>
        </div>
      ) : null}

      <details className="mt-4 rounded-lg border border-border bg-bg-main/70">
        <summary className="cursor-pointer px-3 py-2 text-xs font-medium text-text-main">Source metadata</summary>
        <pre className="max-h-56 overflow-auto border-t border-border p-3 text-xs text-text-muted">{JSON.stringify(node.metadata ?? {}, null, 2)}</pre>
      </details>

      <details className="mt-2 rounded-lg border border-border bg-bg-main/70">
        <summary className="cursor-pointer px-3 py-2 text-xs font-medium text-text-main">Selected buckets</summary>
        <pre className="max-h-56 overflow-auto border-t border-border p-3 text-xs text-text-muted">{JSON.stringify(document.selected, null, 2)}</pre>
      </details>
    </section>
  );
}

function isLaunchSurfaceNode(node: ApiProfileGraphNode): boolean {
  const metadata = node.metadata ?? {};
  const nested = typeof metadata.metadata === 'object' && metadata.metadata ? metadata.metadata as Record<string, unknown> : {};
  const componentType = String(metadata.component_type || nested.component_type || '').toLowerCase();
  const launchCandidate = typeof metadata.launch === 'object' && metadata.launch
    ? metadata.launch as Record<string, unknown>
    : typeof nested.launch === 'object' && nested.launch
      ? nested.launch as Record<string, unknown>
      : {};
  return componentType === 'frontend' && String(launchCandidate.kind || '').toLowerCase() === 'desktop_app';
}
