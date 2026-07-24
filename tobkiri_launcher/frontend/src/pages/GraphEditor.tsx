import { useEffect, useMemo, useRef, useState } from 'react';
import { Braces, CheckCircle2, ListTree, Save, TriangleAlert } from 'lucide-react';

import { Button } from '@/src/components/ui/Button';
import {
  compileCapabilityGraph,
  fetchCapabilityGraph,
  fetchCapabilityGraphs,
  fetchCapabilityProfiles,
  saveCapabilityGraph,
  validateCapabilityGraph,
} from '@/src/lib/api';
import type {
  ApiCapabilityGraph,
  ApiCapabilityProfile,
  CapabilityGraphCompileResponseData,
} from '@/src/lib/apiTypes';
import { cn } from '@/src/lib/utils';
import { useAppStore } from '@/src/store';
import { InlineLoadError } from '@/src/components/ui/InlineLoadError';
import { TobkiriLoader, TobkiriLoadingMark } from '@/src/components/ui/TobkiriLoader';

type GraphViewMode = 'readable' | 'json';

function formatGraph(graph: ApiCapabilityGraph | null): string {
  if (!graph) return '';
  return JSON.stringify({
    version: 'rumi.graph.v1',
    graph_id: graph.graph_id,
    display_name: graph.display_name ?? {en: graph.label},
    description: graph.description ?? {},
    nodes: graph.nodes,
    edges: graph.edges,
    metadata: graph.metadata ?? {},
  }, null, 2);
}

function ReadableCapabilityGraph({ graph }: { graph: Record<string, unknown> | null }) {
  if (!graph) {
    return (
      <div className="flex h-full min-h-[520px] items-center justify-center bg-bg-main p-6">
        <div className="max-w-sm rounded-xl border border-amber-500/30 bg-amber-500/10 p-4 text-sm text-text-muted">
          <div className="flex items-center gap-2 font-medium text-text-main">
            <TriangleAlert className="h-4 w-4 text-amber-500" />
            Invalid JSON
          </div>
        </div>
      </div>
    );
  }

  const nodes = arrayOfRecords(graph.nodes);
  const edges = arrayOfRecords(graph.edges);
  const metadata = recordValue(graph.metadata);

  return (
    <div className="h-full min-h-[520px] overflow-y-auto bg-bg-main p-5">
      <div className="rounded-xl border border-border bg-bg-card p-4">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div className="min-w-0">
            <div className="text-xs font-medium uppercase tracking-[0.16em] text-text-muted">
              {readableValue(graph.graph_id || 'draft')}
            </div>
            <h2 className="mt-1 break-words text-xl font-semibold text-text-main">
              {localizedValue(graph.display_name) || readableValue(graph.label || graph.graph_id || 'Untitled graph')}
            </h2>
            {localizedValue(graph.description) || graph.description_label ? (
              <p className="mt-2 max-w-3xl text-sm text-text-muted">
                {localizedValue(graph.description) || readableValue(graph.description_label)}
              </p>
            ) : null}
          </div>
          <div className="grid grid-cols-2 gap-2 text-center text-xs">
            <Metric label="Nodes" value={String(nodes.length)} />
            <Metric label="Edges" value={String(edges.length)} />
          </div>
        </div>
      </div>

      <div className="mt-4 grid gap-4 2xl:grid-cols-[minmax(0,1fr)_minmax(0,0.85fr)]">
        <section className="rounded-xl border border-border bg-bg-card p-4">
          <div className="mb-3 flex items-center justify-between gap-3">
            <h3 className="text-sm font-semibold text-text-main">Nodes</h3>
            <span className="text-xs text-text-muted">{nodes.length}</span>
          </div>
          <div className="grid gap-3 lg:grid-cols-2">
            {nodes.map((node, index) => (
              <article key={readableValue(node.id || index)} className="rounded-lg border border-border bg-bg-main p-3">
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <div className="truncate text-sm font-semibold text-text-main">
                      {localizedValue(node.display_name) || readableValue(node.id || node.ref || 'node')}
                    </div>
                    <div className="truncate font-mono text-xs text-text-muted">
                      {readableValue(node.ref || node.id)}
                    </div>
                  </div>
                  {node.kind ? (
                    <span className="shrink-0 rounded-full border border-border px-2 py-0.5 text-[11px] text-text-muted">
                      {readableValue(node.kind)}
                    </span>
                  ) : null}
                </div>
                {recordValue(node.metadata) ? (
                  <MetadataPreview metadata={recordValue(node.metadata)!} />
                ) : null}
              </article>
            ))}
            {!nodes.length ? <EmptyReadableState label="No nodes in this graph." /> : null}
          </div>
        </section>

        <section className="rounded-xl border border-border bg-bg-card p-4">
          <div className="mb-3 flex items-center justify-between gap-3">
            <h3 className="text-sm font-semibold text-text-main">Edges</h3>
            <span className="text-xs text-text-muted">{edges.length}</span>
          </div>
          <div className="space-y-2">
            {edges.map((edge, index) => (
              <article key={readableValue(edge.id || index)} className="rounded-lg border border-border bg-bg-main p-3 text-sm">
                <div className="flex flex-wrap items-center gap-2 text-text-main">
                  <span className="break-all font-mono text-xs">{readableValue(edge.from || edge.from_id || '--')}</span>
                  <span className="text-text-muted">-&gt;</span>
                  <span className="break-all font-mono text-xs">{readableValue(edge.to || edge.to_id || '--')}</span>
                </div>
                <div className="mt-2 flex flex-wrap gap-2 text-[11px] text-text-muted">
                  <span className="rounded-full border border-border px-2 py-0.5">{readableValue(edge.kind || 'edge')}</span>
                  {edge.id ? <span className="rounded-full border border-border px-2 py-0.5">{readableValue(edge.id)}</span> : null}
                </div>
              </article>
            ))}
            {!edges.length ? <EmptyReadableState label="No edges in this graph." /> : null}
          </div>
        </section>
      </div>

      {metadata && Object.keys(metadata).length ? (
        <section className="mt-4 rounded-xl border border-border bg-bg-card p-4">
          <h3 className="text-sm font-semibold text-text-main">Metadata</h3>
          <MetadataPreview metadata={metadata} expanded />
        </section>
      ) : null}
    </div>
  );
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div className="min-w-[76px] rounded-lg border border-border bg-bg-main px-3 py-2">
      <div className="text-[11px] text-text-muted">{label}</div>
      <div className="mt-0.5 text-base font-semibold text-text-main">{value}</div>
    </div>
  );
}

function EmptyReadableState({ label }: { label: string }) {
  return (
    <div className="rounded-lg border border-dashed border-border px-4 py-8 text-center text-sm text-text-muted">
      {label}
    </div>
  );
}

function MetadataPreview({ metadata, expanded = false }: { metadata: Record<string, unknown>; expanded?: boolean }) {
  const entries = Object.entries(metadata).slice(0, expanded ? 12 : 4);
  if (!entries.length) {
    return null;
  }
  return (
    <dl className="mt-3 grid gap-2 text-xs">
      {entries.map(([key, value]) => (
        <div key={key} className="min-w-0 rounded-md border border-border bg-bg-hover/40 px-2 py-1.5">
          <dt className="font-medium text-text-main">{key}</dt>
          <dd className="mt-0.5 break-words text-text-muted">{readableValue(value)}</dd>
        </div>
      ))}
    </dl>
  );
}

function arrayOfRecords(value: unknown): Record<string, unknown>[] {
  return Array.isArray(value) ? value.filter(isRecord) : [];
}

function recordValue(value: unknown): Record<string, unknown> | null {
  return isRecord(value) ? value : null;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function localizedValue(value: unknown): string {
  if (typeof value === 'string') {
    return value;
  }
  if (!isRecord(value)) {
    return '';
  }
  const preferred = value.en || value.ja || Object.values(value).find((item) => typeof item === 'string');
  return typeof preferred === 'string' ? preferred : '';
}

function readableValue(value: unknown): string {
  if (value === null || value === undefined) {
    return '--';
  }
  if (typeof value === 'string') {
    return value;
  }
  if (typeof value === 'number' || typeof value === 'boolean') {
    return String(value);
  }
  if (Array.isArray(value)) {
    return value.map(readableValue).join(', ');
  }
  return JSON.stringify(value);
}

export function GraphEditor() {
  const addToast = useAppStore(state => state.addToast);
  const [profiles, setProfiles] = useState<ApiCapabilityProfile[]>([]);
  const [graphs, setGraphs] = useState<ApiCapabilityGraph[]>([]);
  const [selectedProfileId, setSelectedProfileId] = useState('');
  const [selectedGraphId, setSelectedGraphId] = useState('');
  const [loadedGraphId, setLoadedGraphId] = useState('');
  const [source, setSource] = useState('');
  const [baselineSource, setBaselineSource] = useState('');
  const [viewMode, setViewMode] = useState<GraphViewMode>('readable');
  const [preview, setPreview] = useState<CapabilityGraphCompileResponseData | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [graphLoading, setGraphLoading] = useState(false);
  const [failedGraphId, setFailedGraphId] = useState('');
  const graphRequestRef = useRef(0);

  const parsedGraph = useMemo(() => {
    try {
      const parsed = JSON.parse(source);
      return parsed && typeof parsed === 'object' ? parsed as Record<string, unknown> : null;
    } catch {
      return null;
    }
  }, [source]);
  const sourceGraphId = typeof parsedGraph?.graph_id === 'string' ? parsedGraph.graph_id : '';
  const graphMatchesSelection = Boolean(
    selectedGraphId
      && loadedGraphId === selectedGraphId
      && sourceGraphId === selectedGraphId,
  );

  useEffect(() => {
    let mounted = true;
    async function load() {
      setLoading(true);
      try {
        const [profileData, graphData] = await Promise.all([
          fetchCapabilityProfiles(),
          fetchCapabilityGraphs(),
        ]);
        if (!mounted) return;
        setProfiles(profileData.profiles);
        setGraphs(graphData.graphs);
        setSelectedProfileId(profileData.profiles[0]?.profile_id ?? '');
        const graph = graphData.graphs[0] ?? null;
        setSelectedGraphId(graph?.graph_id ?? '');
        setLoadedGraphId(graph?.graph_id ?? '');
        const nextSource = formatGraph(graph);
        setSource(nextSource);
        setBaselineSource(nextSource);
        setLoadError(null);
        setFailedGraphId('');
      } catch (error) {
        const message = error instanceof Error ? error.message : 'Failed to load graphs';
        setLoadError(message);
        addToast(message, 'error');
      } finally {
        if (mounted) setLoading(false);
      }
    }
    void load();
    return () => {
      mounted = false;
    };
  }, [addToast]);

  async function loadGraph(graphId: string) {
    if (graphId === selectedGraphId) return;
    if (graphMatchesSelection && source !== baselineSource) {
      const discard = window.confirm('Discard unsaved graph edits and switch graphs?');
      if (!discard) return;
    }
    if (!graphId) {
      setSelectedGraphId('');
      setLoadedGraphId('');
      setSource('');
      setBaselineSource('');
      return;
    }
    const requestId = graphRequestRef.current + 1;
    graphRequestRef.current = requestId;
    setGraphLoading(true);
    setLoadError(null);
    setFailedGraphId('');
    try {
      const result = await fetchCapabilityGraph(graphId);
      if (graphRequestRef.current !== requestId) return;
      setSelectedGraphId(graphId);
      setLoadedGraphId(result.graph.graph_id);
      const nextSource = formatGraph(result.graph);
      setSource(nextSource);
      setBaselineSource(nextSource);
      setPreview(null);
    } catch (error) {
      if (graphRequestRef.current !== requestId) return;
      const message = error instanceof Error ? error.message : 'Failed to load graph';
      setLoadError(message);
      setFailedGraphId(graphId);
      addToast(message, 'error');
    } finally {
      if (graphRequestRef.current === requestId) setGraphLoading(false);
    }
  }

  async function runPreview(kind: 'validate' | 'compile') {
    if (!parsedGraph || !selectedProfileId || !graphMatchesSelection) {
      addToast('Graph JSON and profile are required', 'error');
      return;
    }
    setBusy(true);
    try {
      const graphId = selectedGraphId;
      const result = kind === 'validate'
        ? await validateCapabilityGraph(graphId, selectedProfileId, parsedGraph)
        : await compileCapabilityGraph(graphId, selectedProfileId, parsedGraph);
      setPreview(result);
    } catch (error) {
      addToast(error instanceof Error ? error.message : 'Preview failed', 'error');
    } finally {
      setBusy(false);
    }
  }

  async function saveGraph() {
    if (!parsedGraph || !graphMatchesSelection) {
      addToast('Loaded graph and editor source do not match', 'error');
      return;
    }
    setBusy(true);
    try {
      const created = !graphs.some(graph => graph.graph_id === parsedGraph.graph_id);
      const saved = await saveCapabilityGraph(parsedGraph, created);
      addToast(created ? 'Graph created' : 'Graph saved', 'success');
      setSelectedGraphId(saved.graph.graph_id);
      setLoadedGraphId(saved.graph.graph_id);
      const nextSource = formatGraph(saved.graph);
      setSource(nextSource);
      setBaselineSource(nextSource);
      const graphData = await fetchCapabilityGraphs();
      setGraphs(graphData.graphs);
    } catch (error) {
      addToast(error instanceof Error ? error.message : 'Save failed', 'error');
    } finally {
      setBusy(false);
    }
  }

  if (loading) {
    return <TobkiriLoader label="Loading graphs" />;
  }

  return (
    <div className="h-full flex flex-col">
      <div className="border-b border-border px-6 py-4 flex items-center justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold text-text-main">Capability Graphs</h1>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <div className="inline-flex rounded-lg border border-border bg-bg-main p-1">
            <button
              type="button"
              className={cn(
                'inline-flex h-8 items-center gap-1.5 rounded-md px-3 text-xs font-medium transition-colors',
                viewMode === 'readable' ? 'bg-bg-hover text-text-main' : 'text-text-muted hover:text-text-main',
              )}
              onClick={() => setViewMode('readable')}
            >
              <ListTree className="h-3.5 w-3.5" />
              Readable
            </button>
            <button
              type="button"
              className={cn(
                'inline-flex h-8 items-center gap-1.5 rounded-md px-3 text-xs font-medium transition-colors',
                viewMode === 'json' ? 'bg-bg-hover text-text-main' : 'text-text-muted hover:text-text-main',
              )}
              onClick={() => setViewMode('json')}
            >
              <Braces className="h-3.5 w-3.5" />
              JSON
            </button>
          </div>
          <Button
            onClick={saveGraph}
            disabled={busy || graphLoading || !parsedGraph || !graphMatchesSelection || viewMode !== 'json'}
            title={viewMode === 'json' ? 'Save JSON graph' : 'Switch to JSON to edit and save'}
          >
            <Save className="w-4 h-4 mr-2" />
            Save JSON
          </Button>
        </div>
      </div>

      <div className="grid min-h-0 flex-1 2xl:grid-cols-[260px_minmax(0,1fr)_340px]">
        <aside className="space-y-4 overflow-y-auto border-b border-border p-4 2xl:border-b-0 2xl:border-r">
          <label className="block text-xs font-medium text-text-muted">Profile</label>
          <select
            className="rumi-select w-full rounded-md border border-border px-3 py-2 pr-9 text-sm"
            value={selectedProfileId}
            onChange={event => setSelectedProfileId(event.target.value)}
          >
            {profiles.map(profile => (
              <option key={profile.profile_id} value={profile.profile_id}>{profile.label}</option>
            ))}
          </select>

          <label className="block text-xs font-medium text-text-muted">Graph</label>
          <select
            className="rumi-select w-full rounded-md border border-border px-3 py-2 pr-9 text-sm"
            value={selectedGraphId}
            onChange={event => void loadGraph(event.target.value)}
          >
            {graphs.map(graph => (
              <option key={graph.graph_id} value={graph.graph_id}>{graph.label}</option>
            ))}
          </select>

          <div className="flex gap-2">
            <Button variant="secondary" onClick={() => void runPreview('validate')} disabled={busy || graphLoading || !graphMatchesSelection}>
              Validate
            </Button>
            <Button variant="secondary" onClick={() => void runPreview('compile')} disabled={busy || graphLoading || !graphMatchesSelection}>
              Compile
            </Button>
          </div>
        </aside>

        <main className="min-h-0">
          {loadError ? (
            <div className="p-4">
              <InlineLoadError
                title={graphs.length ? 'Graph could not be loaded' : 'Capability graphs could not be loaded'}
                message={loadError}
                onRetry={() => graphs.length
                  ? void loadGraph(failedGraphId || selectedGraphId || graphs[0]?.graph_id || '')
                  : window.location.reload()}
                retrying={graphLoading || loading}
                stale={Boolean(source)}
              />
            </div>
          ) : null}
          {graphLoading ? (
            <div role="status" className="flex items-center gap-2 border-b border-border px-4 py-2 text-sm text-text-muted">
              <TobkiriLoadingMark /> Loading selected graph…
            </div>
          ) : null}
          {!loadError && graphs.length === 0 ? (
            <div className="flex min-h-[520px] items-center justify-center p-6 text-center">
              <div>
                <h2 className="text-base font-semibold text-text-main">No capability graphs</h2>
                <p className="mt-1 text-sm text-text-muted">Graphs available to this runtime will appear here.</p>
              </div>
            </div>
          ) : viewMode === 'json' ? (
            <textarea
              className="h-full min-h-[520px] w-full resize-none bg-bg-main p-4 font-mono text-sm text-text-main outline-none"
              value={source}
              spellCheck={false}
              onChange={event => setSource(event.target.value)}
              disabled={graphLoading || !loadedGraphId}
            />
          ) : (
            <ReadableCapabilityGraph graph={parsedGraph} />
          )}
        </main>

        <aside className="overflow-y-auto border-t border-border p-4 2xl:border-l 2xl:border-t-0">
          <h2 className="text-sm font-semibold text-text-main">Preview</h2>
          {!preview ? (
            <div className="mt-4 text-sm text-text-muted">No preview yet</div>
          ) : (
            <div className="mt-4 space-y-3">
              <div className="flex items-center gap-2 text-sm">
                {preview.ok ? <CheckCircle2 className="w-4 h-4 text-green-600" /> : <TriangleAlert className="w-4 h-4 text-amber-600" />}
                <span>{preview.ok ? 'Ready' : 'Needs attention'}</span>
              </div>
              {preview.surface_launch_target && (
                <div className="rounded-md border border-border bg-bg-main p-3">
                  <div className="text-xs font-semibold text-text-main">Launch target</div>
                  <div className="mt-1 text-xs text-text-muted">
                    {preview.surface_launch_target.pack_id}
                    {' / '}
                    {preview.surface_launch_target.node_id ?? preview.surface_launch_target.node_instance_id ?? 'desktop_app'}
                  </div>
                </div>
              )}
              {preview.diagnostics.map((item, index) => (
                <div key={`${item.code}-${index}`} className="rounded-md border border-border p-3 text-xs">
                  <div className="font-medium text-text-main">{item.code}</div>
                  <div className="mt-1 text-text-muted">{item.message}</div>
                </div>
              ))}
            </div>
          )}
        </aside>
      </div>
    </div>
  );
}
