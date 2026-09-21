import { useEffect, useMemo, useState } from 'react';
import {
  Boxes,
  CheckCircle2,
  Copy,
  GitBranch,
  Loader2,
  Plug,
  RefreshCw,
  Search,
  ShieldCheck,
  TriangleAlert,
} from 'lucide-react';

import { Badge } from '@/src/components/ui/Badge';
import { Button } from '@/src/components/ui/Button';
import { Input } from '@/src/components/ui/Input';
import { InlineLoadError } from '@/src/components/ui/InlineLoadError';
import {
  cloneCapabilityProfile,
  compileCapabilityGraph,
  fetchCapabilityGraphs,
  fetchCapabilityProfileNodes,
  fetchCapabilityProfiles,
  validateCapabilityGraph,
} from '@/src/lib/api';
import type {
  ApiCapabilityGraph,
  ApiCapabilityNode,
  ApiCapabilityProfile,
  CapabilityGraphCompileResponseData,
  StartupProfileRelationship,
} from '@/src/lib/apiTypes';
import {
  capabilityNodeDescription,
  capabilityNodeLabel,
  capabilityNodePorts,
  capabilityPortLabel,
  capabilityPortStandards,
  normalizeCapabilityProfileNodes,
} from '@/src/lib/nodeCatalog';
import { cn } from '@/src/lib/utils';
import { useAppStore } from '@/src/store';

function statusVariant(status?: string): 'default' | 'secondary' | 'destructive' | 'outline' {
  if (status === 'ready') return 'default';
  if (status === 'disabled') return 'secondary';
  if (status === 'missing_config' || status === 'missing_node' || status === 'unapproved') {
    return 'destructive';
  }
  return 'outline';
}

function canCloneProfile(profile: ApiCapabilityProfile | null): boolean {
  return profile?.permissions?.can_create_profile === true;
}

export function NodeManager() {
  const addToast = useAppStore(state => state.addToast);
  const [profiles, setProfiles] = useState<ApiCapabilityProfile[]>([]);
  const [graphs, setGraphs] = useState<ApiCapabilityGraph[]>([]);
  const [selectedProfileId, setSelectedProfileId] = useState('');
  const [selectedGraphId, setSelectedGraphId] = useState('');
  const [nodes, setNodes] = useState<ApiCapabilityNode[]>([]);
  const [paletteNodes, setPaletteNodes] = useState<ApiCapabilityNode[]>([]);
  const [selectedNodeId, setSelectedNodeId] = useState('');
  const [relationship, setRelationship] = useState<StartupProfileRelationship | null>(null);
  const [search, setSearch] = useState('');
  const [loading, setLoading] = useState(true);
  const [profileLoading, setProfileLoading] = useState(false);
  const [preview, setPreview] = useState<CapabilityGraphCompileResponseData | null>(null);
  const [initialError, setInitialError] = useState<string | null>(null);
  const [profileError, setProfileError] = useState<string | null>(null);

  const selectedProfile = profiles.find(profile => profile.profile_id === selectedProfileId) ?? null;
  const selectedNode = nodes.find(node => node.node_id === selectedNodeId) ?? null;

  const filteredNodes = useMemo(() => {
    const term = search.trim().toLowerCase();
    if (!term) return nodes;
    return nodes.filter(node => {
      const haystack = [
        node.node_id,
        capabilityNodeLabel(node),
        capabilityNodeDescription(node),
        String(node.metadata?.category ?? ''),
      ].join(' ').toLowerCase();
      return haystack.includes(term);
    });
  }, [nodes, search]);

  const loadInitial = async () => {
    setLoading(true);
    try {
      const [profileData, graphData] = await Promise.all([
        fetchCapabilityProfiles(),
        fetchCapabilityGraphs(),
      ]);
      setProfiles(profileData.profiles);
      setRelationship(profileData.startup_profile_relationship);
      setGraphs(graphData.graphs);
      const nextProfile = selectedProfileId || profileData.profiles[0]?.profile_id || '';
      const nextGraph = selectedGraphId || profileData.profiles[0]?.default_graph || graphData.graphs[0]?.graph_id || '';
      setSelectedProfileId(nextProfile);
      setSelectedGraphId(nextGraph);
      setInitialError(null);
    } catch (error) {
      const message = error instanceof Error ? error.message : 'Failed to load capability profiles';
      setInitialError(message);
      addToast(message, 'error');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void loadInitial();
  }, []);

  useEffect(() => {
    if (!selectedProfileId) return;
    let cancelled = false;
    setProfileLoading(true);
    setPreview(null);
    fetchCapabilityProfileNodes(selectedProfileId)
      .then(data => {
        if (cancelled) return;
        const normalized = normalizeCapabilityProfileNodes(
          data,
          profiles.find(profile => profile.profile_id === selectedProfileId) ?? null,
        );
        setNodes(normalized.nodes);
        setPaletteNodes(normalized.paletteNodes);
        setSelectedNodeId(current => (
          normalized.nodes.some(node => node.node_id === current)
            ? current
            : normalized.nodes[0]?.node_id ?? ''
        ));
        if (!selectedGraphId && normalized.profile?.default_graph) {
          setSelectedGraphId(normalized.profile.default_graph);
        }
        setProfileError(null);
      })
      .catch(error => {
        if (!cancelled) {
          const message = error instanceof Error ? error.message : 'Failed to load profile nodes';
          setProfileError(message);
          addToast(message, 'error');
        }
      })
      .finally(() => {
        if (!cancelled) setProfileLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [addToast, profiles, selectedGraphId, selectedProfileId]);

  const runValidate = async () => {
    if (!selectedGraphId || !selectedProfileId) return;
    try {
      const result = await validateCapabilityGraph(selectedGraphId, selectedProfileId);
      setPreview(result);
      addToast(result.ok ? 'Graph validation passed' : 'Graph validation failed', result.ok ? 'success' : 'error');
    } catch (error) {
      addToast(error instanceof Error ? error.message : 'Graph validation failed', 'error');
    }
  };

  const runCompile = async () => {
    if (!selectedGraphId || !selectedProfileId) return;
    try {
      const result = await compileCapabilityGraph(selectedGraphId, selectedProfileId);
      setPreview(result);
      addToast(result.ok ? 'Compile preview ready' : 'Compile preview failed', result.ok ? 'success' : 'error');
    } catch (error) {
      addToast(error instanceof Error ? error.message : 'Compile preview failed', 'error');
    }
  };

  const cloneProfile = async () => {
    if (!selectedProfile) return;
    const profileId = `${selectedProfile.profile_id}_copy`;
    try {
      const result = await cloneCapabilityProfile(selectedProfile.profile_id, {
        profile_id: profileId,
        display_name: `${selectedProfile.label} Copy`,
      });
      addToast(`${result.profile.label} created`, 'success');
      await loadInitial();
      setSelectedProfileId(result.profile.profile_id);
    } catch (error) {
      addToast(error instanceof Error ? error.message : 'Profile clone failed', 'error');
    }
  };

  if (loading) {
    return (
      <div className="flex flex-1 items-center justify-center bg-bg-main">
        <div className="flex flex-col items-center gap-3">
          <Loader2 className="h-8 w-8 animate-spin text-accent" />
          <span className="text-sm text-text-muted">Loading capability graph</span>
        </div>
      </div>
    );
  }

  if (initialError && profiles.length === 0 && graphs.length === 0) {
    return (
      <div className="flex flex-1 items-center justify-center bg-bg-main p-6">
        <div className="w-full max-w-xl">
          <InlineLoadError
            title="Node Manager could not be loaded"
            message={initialError}
            onRetry={() => void loadInitial()}
            retrying={loading}
          />
        </div>
      </div>
    );
  }

  return (
    <div className="flex flex-1 flex-col gap-5 overflow-y-auto bg-bg-main p-6 animate-in fade-in slide-in-from-bottom-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold text-text-main">Node Manager</h1>
          <div className="mt-1 flex flex-wrap items-center gap-2 text-xs text-text-muted">
            <span>{relationship?.launch_time_source_of_truth ?? 'StartupProfileManager'}</span>
            <span>-</span>
            <span>{relationship?.capability_graph_profiles_role ?? 'graph_runtime_presets'}</span>
          </div>
        </div>
        <Button variant="outline" size="sm" onClick={() => void loadInitial()}>
          <RefreshCw className="mr-2 h-4 w-4" />
          Refresh
        </Button>
      </div>

      {initialError ? (
        <InlineLoadError title="Refresh failed" message={initialError} onRetry={() => void loadInitial()} retrying={loading} stale />
      ) : null}
      {profileError ? (
        <InlineLoadError
          title="Profile nodes could not be refreshed"
          message={profileError}
          onRetry={() => {
            const current = selectedProfileId;
            setSelectedProfileId('');
            queueMicrotask(() => setSelectedProfileId(current));
          }}
          retrying={profileLoading}
          stale={nodes.length > 0}
        />
      ) : null}

      <div className="grid gap-3 lg:grid-cols-[minmax(220px,280px)_1fr_minmax(280px,360px)]">
        <section className="rounded-lg border border-border bg-bg-card p-4">
          <div className="mb-3 flex items-center justify-between gap-2">
            <h2 className="text-sm font-semibold text-text-main">Profiles</h2>
            {canCloneProfile(selectedProfile) && (
              <Button variant="ghost" size="icon" title="Clone profile" onClick={() => void cloneProfile()}>
                <Copy className="h-4 w-4" />
              </Button>
            )}
          </div>
          <div className="space-y-2">
            {profiles.map(profile => (
              <button
                key={profile.profile_id}
                className={cn(
                  "w-full rounded-md border px-3 py-2 text-left transition-colors",
                  selectedProfileId === profile.profile_id
                    ? "border-text-muted/50 bg-bg-hover text-text-main"
                    : "border-border text-text-muted hover:bg-bg-hover hover:text-text-main",
                )}
                onClick={() => setSelectedProfileId(profile.profile_id)}
              >
                <div className="truncate text-sm font-medium">{profile.label}</div>
                <div className="truncate text-xs">{profile.profile_id}</div>
              </button>
            ))}
          </div>
        </section>

        <section className="rounded-lg border border-border bg-bg-card p-4">
          <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
            <div className="flex items-center gap-2">
              <Boxes className="h-4 w-4 text-accent" />
              <h2 className="text-sm font-semibold text-text-main">Catalog</h2>
              {profileLoading && <Loader2 className="h-4 w-4 animate-spin text-text-muted" />}
            </div>
            <div className="relative w-full sm:w-64">
              <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-text-muted" />
              <Input value={search} onChange={event => setSearch(event.target.value)} className="pl-9" />
            </div>
          </div>

          {profileError && nodes.length === 0 ? (
            <div className="border border-dashed border-border p-6 text-center text-sm text-text-muted">
              Node counts are unavailable until this profile loads successfully.
            </div>
          ) : <>
            <div className="mb-4 flex flex-wrap gap-2">
              <Badge variant="outline">{nodes.length} installed</Badge>
              <Badge variant="default">{paletteNodes.length} palette</Badge>
              <Badge variant="secondary">{nodes.filter(node => node.state?.status === 'disabled').length} disabled</Badge>
            </div>

            <div className="grid gap-2 2xl:grid-cols-2">
            {filteredNodes.map(node => (
              <button
                key={node.node_id}
                className={cn(
                  "block w-full px-3 py-3 text-left transition-colors",
                  selectedNodeId === node.node_id
                    ? "bg-bg-hover"
                    : "hover:bg-bg-hover/70",
                )}
                onClick={() => setSelectedNodeId(node.node_id)}
              >
                <div className="flex items-start justify-between gap-2">
                  <div className="min-w-0">
                    <div className="truncate text-sm font-semibold text-text-main">{node.label}</div>
                    <div className="mt-0.5 truncate font-mono text-[10px] text-text-muted">{node.node_id}</div>
                  </div>
                  <Badge variant={statusVariant(node.state?.status)}>{node.state?.status ?? 'unknown'}</Badge>
                </div>
                <p className="mt-2 line-clamp-2 text-xs leading-5 text-text-muted">{capabilityNodeDescription(node)}</p>
                <div className="mt-2 flex flex-wrap gap-1">
                  {capabilityNodePorts(node).slice(0, 4).map(port => (
                    <span key={port.id} className="rounded border border-border px-1.5 py-0.5 font-mono text-[10px] text-text-muted">
                      {port.direction === 'input' ? 'in' : port.direction === 'output' ? 'out' : 'bi'}:{port.id}
                    </span>
                  ))}
                </div>
              </button>
            ))}
            </div>
          </>}
        </section>

        <aside className="flex flex-col gap-3">
          <section className="rounded-lg border border-border bg-bg-card p-4">
            <div className="mb-3 flex items-center gap-2">
              <Plug className="h-4 w-4 text-accent" />
              <h2 className="text-sm font-semibold text-text-main">Details</h2>
            </div>
            {selectedNode ? (
              <div className="space-y-3">
                <div>
                  <div className="text-base font-semibold text-text-main">{capabilityNodeLabel(selectedNode)}</div>
                  <div className="break-all text-xs text-text-muted">{selectedNode.node_id}</div>
                </div>
                <div className="flex flex-wrap gap-2">
                  <Badge variant={statusVariant(selectedNode.state?.status)}>{selectedNode.state?.status ?? 'unknown'}</Badge>
                  <Badge variant="outline">{String(selectedNode.metadata?.category ?? selectedNode.kind)}</Badge>
                </div>
                {selectedNode.state?.missing?.length ? (
                  <div className="rounded-md border border-red-500/40 bg-red-500/10 p-2 text-xs text-red-300">
                    {selectedNode.state.missing.join(', ')}
                  </div>
                ) : null}
                <div className="space-y-2">
                  {capabilityNodePorts(selectedNode).map(port => (
                    <div key={port.id} className="rounded-md border border-border p-2">
                      <div className="flex items-center justify-between gap-2">
                        <span className="text-sm font-medium text-text-main">{capabilityPortLabel(port)}</span>
                        <span className="text-xs text-text-muted">{port.direction}</span>
                      </div>
                      <div className="mt-2 flex flex-wrap gap-1">
                        {capabilityPortStandards(port).map(standard => (
                          <span key={standard} className="rounded bg-bg-hover px-1.5 py-0.5 text-[11px] text-text-muted">
                            {standard}
                          </span>
                        ))}
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            ) : (
              <div className="text-sm text-text-muted">No node selected</div>
            )}
          </section>

          <section className="rounded-lg border border-border bg-bg-card p-4">
            <div className="mb-3 flex items-center gap-2">
              <GitBranch className="h-4 w-4 text-accent" />
              <h2 className="text-sm font-semibold text-text-main">Graphs</h2>
            </div>
            <select
              value={selectedGraphId}
              onChange={event => setSelectedGraphId(event.target.value)}
              className="mb-3 h-10 w-full rounded-md border border-border bg-bg-main px-3 text-sm text-text-main"
            >
              {graphs.map(graph => (
                <option key={graph.graph_id} value={graph.graph_id}>{graph.label}</option>
              ))}
            </select>
            <div className="grid grid-cols-2 gap-2">
              <Button variant="outline" size="sm" onClick={() => void runValidate()} disabled={!selectedGraphId || !selectedProfileId}>
                <ShieldCheck className="mr-2 h-4 w-4" />
                Validate
              </Button>
              <Button size="sm" onClick={() => void runCompile()} disabled={!selectedGraphId || !selectedProfileId}>
                <CheckCircle2 className="mr-2 h-4 w-4" />
                Compile
              </Button>
            </div>
            {preview && (
              <div className="mt-3 rounded-md border border-border bg-bg-main p-3">
                <div className="mb-2 flex items-center gap-2 text-sm font-medium text-text-main">
                  {preview.ok ? <CheckCircle2 className="h-4 w-4 text-green-500" /> : <TriangleAlert className="h-4 w-4 text-red-500" />}
                  {preview.ok ? 'OK' : 'Failed'}
                </div>
                {preview.surface_launch_target && (
                  <div className="mb-3 rounded-md border border-border bg-bg-hover p-3">
                    <div className="text-xs font-semibold text-text-main">Launch target</div>
                    <div className="mt-1 text-xs text-text-muted">
                      {preview.surface_launch_target.pack_id}
                      {' / '}
                      {preview.surface_launch_target.node_id ?? preview.surface_launch_target.node_instance_id ?? 'desktop_app'}
                    </div>
                  </div>
                )}
                <pre className="max-h-56 overflow-auto whitespace-pre-wrap break-words text-xs text-text-muted">
                  {JSON.stringify(preview.runtime_profile ?? preview.diagnostics, null, 2)}
                </pre>
              </div>
            )}
          </section>
        </aside>
      </div>
    </div>
  );
}
