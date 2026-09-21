import {useEffect, useMemo, useState} from 'react';
import {useSearchParams} from 'react-router-dom';
import {AlertTriangle, Loader2, Network, RadioTower, Route, Sparkles, Wand2} from 'lucide-react';

import {ProfileGraphCanvas} from '@/src/components/profile-graph/ProfileGraphCanvas';
import {ProfileGraphInspector} from '@/src/components/profile-graph/ProfileGraphInspector';
import {ProfileGraphPalette} from '@/src/components/profile-graph/ProfileGraphPalette';
import {ProfileGraphToolbar} from '@/src/components/profile-graph/ProfileGraphToolbar';
import {Badge} from '@/src/components/ui/Badge';
import {
  compileStartupProfileGraphPreview,
  fetchStartupProfileGraph,
  fetchStartupProfiles,
  launchStartupProfile,
  updateStartupProfileGraph,
} from '@/src/lib/api';
import type {
  ApiProfileGraphAvailableItem,
  ApiProfileGraphDocument,
  ApiProfileGraphNode,
  ApiStartupProfile,
  StartupProfileGraphCompilePreviewResponseData,
  StartupProfileGraphResponseData,
} from '@/src/lib/apiTypes';
import {
  addProfileGraphSelection,
  normalizeProfileGraphDocument,
  PROFILE_GRAPH_CATEGORIES,
  profileGraphNodePrefix,
  profileGraphRequestPayload,
  removeProfileGraphSelection,
  type ProfileGraphCategory,
} from '@/src/lib/profileGraph';
import {cn} from '@/src/lib/utils';
import {useAppStore} from '@/src/store';

type AvailableByCategory = Record<ProfileGraphCategory, ApiProfileGraphAvailableItem[]>;

interface ProfileGraphEditorShellProps {
  profiles: ApiStartupProfile[];
  activeProfileId: string | null;
  selectedProfileId: string;
  graphData: StartupProfileGraphResponseData | null;
  draft: ApiProfileGraphDocument | null;
  preview: StartupProfileGraphCompilePreviewResponseData | null;
  activeCategory: ProfileGraphCategory;
  paletteSearch: string;
  selectedNodeId: string | null;
  loading?: boolean;
  graphLoading?: boolean;
  saving?: boolean;
  previewing?: boolean;
  launching?: boolean;
  error?: string | null;
  onSelectProfile: (profileId: string) => void;
  onCategoryChange: (category: ProfileGraphCategory) => void;
  onPaletteSearchChange: (value: string) => void;
  onAddCandidate: (category: ProfileGraphCategory, item: ApiProfileGraphAvailableItem) => void;
  onSelectNode: (nodeId: string) => void;
  onRemoveSelection: (category: ProfileGraphCategory, ref: string) => void;
  onApply: () => void;
  onPreview: () => void;
  onLaunch: () => void;
}

export interface ProfileGraphEditorActionApi {
  update: typeof updateStartupProfileGraph;
  preview: typeof compileStartupProfileGraphPreview;
  launch: typeof launchStartupProfile;
}

export function createProfileGraphEditorActions(api: ProfileGraphEditorActionApi) {
  return {
    apply(profileId: string, document: ApiProfileGraphDocument) {
      return api.update(profileId, profileGraphRequestPayload(document));
    },
    preview(profileId: string, document: ApiProfileGraphDocument) {
      return api.preview(profileId, profileGraphRequestPayload(document));
    },
    launch(profileId: string) {
      return api.launch(profileId);
    },
  };
}

export function ProfileGraphEditorShell({
  profiles,
  activeProfileId,
  selectedProfileId,
  graphData,
  draft,
  preview,
  activeCategory,
  paletteSearch,
  selectedNodeId,
  loading,
  graphLoading,
  saving,
  previewing,
  launching,
  error,
  onSelectProfile,
  onCategoryChange,
  onPaletteSearchChange,
  onAddCandidate,
  onSelectNode,
  onRemoveSelection,
  onApply,
  onPreview,
  onLaunch,
}: ProfileGraphEditorShellProps) {
  const available = useMemo<AvailableByCategory>(() => ({
    tools: graphData?.available.tools || [],
    webhooks: graphData?.available.webhooks || [],
    api_routes: graphData?.available.api_routes || [],
    prompts: graphData?.available.prompts || [],
    frontend: graphData?.available.frontend || [],
    flows: graphData?.available.flows || [],
    nodes: graphData?.available.capability_nodes || [],
  }), [graphData]);

  const selectedNode = useMemo<ApiProfileGraphNode | null>(
    () => draft?.nodes.find((node) => node.id === selectedNodeId) ?? null,
    [draft, selectedNodeId],
  );

  const dirty = useMemo(() => {
    if (!draft || !graphData) {
      return false;
    }
    return JSON.stringify(profileGraphRequestPayload(draft)) !== JSON.stringify(profileGraphRequestPayload(
      normalizeProfileGraphDocument(graphData.profile_id, graphData.graph),
    ));
  }, [draft, graphData]);

  const diagnostics = preview?.profile_graph_runtime_preview?.diagnostics?.length
    ? preview.profile_graph_runtime_preview.diagnostics
    : graphData?.diagnostics || [];

  return (
    <div className="flex h-full min-h-0 flex-1 flex-col gap-3 overflow-y-auto bg-bg-main p-4 animate-in fade-in slide-in-from-bottom-4 xl:overflow-hidden">
      <div className="flex shrink-0 flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold text-text-main">Profile Wiring</h1>
          <p className="mt-0.5 text-xs text-text-muted">
            Choose the tools, policy routes, prompts, UI, and launch graph used by this profile.
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {activeProfileId ? <Badge variant="outline">Active: {activeProfileId}</Badge> : null}
          {graphData?.summary ? (
            <>
              <Badge variant="secondary">{graphData.summary.selected_tool_count} tools</Badge>
              <Badge variant="secondary">{graphData.summary.selected_webhook_count} webhooks</Badge>
              <Badge variant="secondary">{graphData.summary.selected_prompt_count} rules</Badge>
            </>
          ) : null}
        </div>
      </div>

      {error ? (
        <div className="rounded-2xl border border-red-500/30 bg-red-500/10 px-4 py-3 text-sm text-red-200">
          {error}
        </div>
      ) : null}

      <div className="grid min-h-0 flex-1 gap-3 xl:grid-cols-[220px_minmax(0,1fr)_270px]">
        <section className="flex min-h-0 flex-col gap-3 overflow-hidden">
          <div className="shrink-0 rounded-xl border border-border bg-bg-card p-3">
            <div className="mb-2 flex items-center gap-2">
              <Sparkles className="h-4 w-4 text-accent" />
              <h2 className="text-xs font-semibold text-text-main">Profile</h2>
            </div>
            <select
              value={selectedProfileId}
              onChange={(event) => onSelectProfile(event.target.value)}
              className="rumi-select h-9 w-full rounded-md border border-border px-3 pr-9 text-xs"
              aria-label="Profile"
            >
              {profiles.map((profile) => <option key={profile.profile_id} value={profile.profile_id}>{profile.name}</option>)}
            </select>
          </div>

          <ProfileGraphPalette
            activeCategory={activeCategory}
            available={available}
            selectedValues={draft?.selected[activeCategory] || []}
            search={paletteSearch}
            onSearchChange={onPaletteSearchChange}
            onCategoryChange={onCategoryChange}
            onAdd={onAddCandidate}
          />
        </section>

        <section className="flex min-h-0 flex-col gap-3 overflow-hidden">
          <ProfileGraphToolbar
            dirty={dirty}
            saving={saving}
            previewing={previewing}
            launching={launching}
            onPreview={onPreview}
            onApply={onApply}
            onLaunch={onLaunch}
          />

          {loading || graphLoading ? (
            <div className="flex min-h-0 flex-1 items-center justify-center rounded-xl border border-border bg-bg-card">
              <div className="flex items-center gap-3 text-text-muted">
                <Loader2 className="h-5 w-5 animate-spin" />
                <span>Loading profile graph</span>
              </div>
            </div>
          ) : (
            <div className="min-h-0 flex-1">
              <ProfileGraphCanvas
                nodes={draft?.nodes || []}
                edges={draft?.edges || []}
                selectedNodeId={selectedNodeId}
                onSelectNode={onSelectNode}
              />
            </div>
          )}

          <div className="hidden">
            <article className="rounded-2xl border border-border bg-bg-card p-4">
              <div className="mb-3 flex items-center gap-2">
                <Wand2 className="h-4 w-4 text-accent" />
                <h2 className="text-sm font-semibold text-text-main">Preview Runtime</h2>
              </div>
              {preview ? (
                <div className="space-y-3 text-sm text-text-muted">
                  <div className="flex flex-wrap gap-2">
                    <Badge variant="default">{preview.compile_preview.ok ? 'Preview ready' : 'Preview has issues'}</Badge>
                    <Badge variant="outline">{String(preview.profile_graph_runtime_preview.prompt_resolution?.selected_prompt_id ?? 'no rule')}</Badge>
                  </div>
                  <div>selected tools: {(preview.profile_graph_runtime_preview.selected?.tools || []).join(', ') || '--'}</div>
                  <div>frontend catalog: {(preview.profile_graph_runtime_preview.selected?.frontend || []).join(', ') || '--'}</div>
                  <div>launch surface: {String(preview.compile_preview.surface_launch_target?.node_id ?? preview.compile_preview.capability_graph?.surface_launch_target?.node_id ?? '--')}</div>
                  <div>launch pack: {String(preview.compile_preview.surface_launch_target?.pack_id ?? preview.compile_preview.capability_graph?.surface_launch_target?.pack_id ?? '--')}</div>
                  <div>api strict mode: {String(Boolean(preview.profile_graph_runtime_preview.api_route_policy?.enforce))}</div>
                </div>
              ) : (
                <p className="text-sm text-text-muted">Run Preview Runtime to inspect the effective rule prompt, tool filter result, launch surface, webhook status, and API policy.</p>
              )}
            </article>

            <article className="rounded-2xl border border-border bg-bg-card p-4">
              <div className="mb-3 flex items-center gap-2">
                <Route className="h-4 w-4 text-accent" />
                <h2 className="text-sm font-semibold text-text-main">Diagnostics</h2>
              </div>
              <div className="space-y-2">
                {diagnostics.length ? diagnostics.map((diagnostic, index) => (
                  <div key={`${diagnostic.code}-${index}`} className="rounded-xl border border-border bg-bg-main/70 px-3 py-2 text-sm text-text-muted">
                    <div className="flex items-center gap-2">
                      <AlertTriangle className="h-4 w-4 text-amber-300" />
                      <span className="font-medium text-text-main">{diagnostic.code}</span>
                    </div>
                    <div className="mt-1 text-xs">{diagnostic.message}</div>
                  </div>
                )) : (
                  <div className="rounded-xl border border-dashed border-border px-3 py-6 text-sm text-text-muted">
                    No diagnostics. This profile graph is currently clean.
                  </div>
                )}
              </div>
            </article>
          </div>
        </section>

        <section className="min-h-0 space-y-3 overflow-y-auto pr-1">
          <ProfileGraphInspector
            document={draft || normalizeProfileGraphDocument(selectedProfileId, null)}
            node={selectedNode}
            preview={preview?.profile_graph_runtime_preview || null}
            onRemoveSelection={onRemoveSelection}
          />

          <article className="rounded-xl border border-border bg-bg-card p-3">
            <div className="flex items-center justify-between gap-2">
              <div className="flex items-center gap-2"><Wand2 className="h-4 w-4 text-accent" /><h2 className="text-sm font-semibold text-text-main">Runtime preview</h2></div>
              {preview ? <Badge variant={preview.compile_preview.ok ? 'success' : 'warning'}>{preview.compile_preview.ok ? 'Ready' : 'Check'}</Badge> : null}
            </div>
            {preview ? (
              <dl className="mt-3 grid gap-2 text-xs">
                <div className="flex justify-between gap-3"><dt className="text-text-muted">Prompt</dt><dd className="truncate text-text-main">{String(preview.profile_graph_runtime_preview.prompt_resolution?.selected_prompt_id ?? 'default')}</dd></div>
                <div className="flex justify-between gap-3"><dt className="text-text-muted">Launch node</dt><dd className="truncate text-text-main">{String(preview.compile_preview.surface_launch_target?.node_id ?? preview.compile_preview.capability_graph?.surface_launch_target?.node_id ?? '--')}</dd></div>
                <div className="flex justify-between gap-3"><dt className="text-text-muted">API policy</dt><dd className="text-text-main">{preview.profile_graph_runtime_preview.api_route_policy?.enforce ? 'Strict' : 'Open'}</dd></div>
              </dl>
            ) : <p className="mt-2 text-xs text-text-muted">Use Preview Runtime to verify the effective selection before applying it.</p>}
          </article>

          {diagnostics.length ? (
            <article className="rounded-xl border border-border bg-bg-card p-3">
              <div className="mb-2 flex items-center gap-2"><AlertTriangle className="h-4 w-4 text-amber-400" /><h2 className="text-sm font-semibold text-text-main">Diagnostics</h2></div>
              <div className="space-y-2">
                {diagnostics.map((diagnostic, index) => (
                  <div key={`${diagnostic.code}-${index}`} className="rounded-lg border border-border bg-bg-main px-2.5 py-2 text-xs text-text-muted">
                    <div className="font-medium text-text-main">{diagnostic.code}</div><div className="mt-0.5">{diagnostic.message}</div>
                  </div>
                ))}
              </div>
            </article>
          ) : null}

          <article className="rounded-2xl border border-border bg-bg-card p-4">
            <div className="mb-3 flex items-center gap-2">
              <RadioTower className="h-4 w-4 text-accent" />
              <h2 className="text-sm font-semibold text-text-main">Selection Summary</h2>
            </div>
            <div className="grid grid-cols-2 gap-3 text-sm text-text-muted">
              {PROFILE_GRAPH_CATEGORIES.map((category) => (
                <div key={category} className="rounded-xl border border-border bg-bg-main/70 px-3 py-2">
                  <div className="text-xs uppercase tracking-[0.16em]">{category.replace('_', ' ')}</div>
                  <div className="mt-1 text-base font-semibold text-text-main">{draft?.selected[category].length || 0}</div>
                </div>
              ))}
            </div>
          </article>

          <article className="rounded-2xl border border-border bg-bg-card p-4">
            <div className="mb-3 flex items-center gap-2">
              <Network className="h-4 w-4 text-accent" />
              <h2 className="text-sm font-semibold text-text-main">Raw Graph</h2>
            </div>
            <details className="rounded-xl border border-border bg-bg-main/70">
              <summary className="cursor-pointer px-3 py-2 text-sm text-text-muted">Show JSON payload</summary>
              <pre className="max-h-[340px] overflow-auto border-t border-border p-3 text-xs text-text-muted">
                {JSON.stringify(profileGraphRequestPayload(draft || normalizeProfileGraphDocument(selectedProfileId, null)), null, 2)}
              </pre>
            </details>
          </article>
        </section>
      </div>
    </div>
  );
}

export function ProfileGraphEditor() {
  const addToast = useAppStore((state) => state.addToast);
  const [searchParams, setSearchParams] = useSearchParams();
  const [profiles, setProfiles] = useState<ApiStartupProfile[]>([]);
  const [activeProfileId, setActiveProfileId] = useState<string | null>(null);
  const [selectedProfileId, setSelectedProfileId] = useState(searchParams.get('profile') || '');
  const [graphData, setGraphData] = useState<StartupProfileGraphResponseData | null>(null);
  const [draft, setDraft] = useState<ApiProfileGraphDocument | null>(null);
  const [preview, setPreview] = useState<StartupProfileGraphCompilePreviewResponseData | null>(null);
  const [activeCategory, setActiveCategory] = useState<ProfileGraphCategory>('tools');
  const [paletteSearch, setPaletteSearch] = useState('');
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [graphLoading, setGraphLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [previewing, setPreviewing] = useState(false);
  const [launching, setLaunching] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const actions = useMemo(() => createProfileGraphEditorActions({
    update: updateStartupProfileGraph,
    preview: compileStartupProfileGraphPreview,
    launch: launchStartupProfile,
  }), []);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    fetchStartupProfiles()
      .then((response) => {
        if (cancelled) {
          return;
        }
        setProfiles(response.profiles);
        setActiveProfileId(response.active_profile_id);
        const preferred = searchParams.get('profile') || response.active_profile_id || response.profiles[0]?.profile_id || '';
        setSelectedProfileId((current) => current || preferred);
      })
      .catch((fetchError) => {
        if (!cancelled) {
          const message = fetchError instanceof Error ? fetchError.message : 'Failed to load startup profiles';
          setError(message);
          addToast(message, 'error');
        }
      })
      .finally(() => {
        if (!cancelled) {
          setLoading(false);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [addToast, searchParams]);

  useEffect(() => {
    if (!selectedProfileId) {
      return;
    }
    let cancelled = false;
    setGraphLoading(true);
    setError(null);
    fetchStartupProfileGraph(selectedProfileId)
      .then((response) => {
        if (cancelled) {
          return;
        }
        setGraphData(response);
        const normalized = normalizeProfileGraphDocument(response.profile_id, response.graph);
        setDraft(normalized);
        setPreview(null);
        setSelectedNodeId(`profile:${response.profile_id}`);
      })
      .catch((fetchError) => {
        if (!cancelled) {
          const message = fetchError instanceof Error ? fetchError.message : 'Failed to load profile graph';
          setError(message);
          addToast(message, 'error');
        }
      })
      .finally(() => {
        if (!cancelled) {
          setGraphLoading(false);
        }
      });
  }, [addToast, selectedProfileId]);

  const setProfileParam = (profileId: string) => {
    const next = new URLSearchParams(searchParams);
    next.set('profile', profileId);
    setSearchParams(next, {replace: true});
  };

  const handleSelectProfile = (profileId: string) => {
    setSelectedProfileId(profileId);
    setProfileParam(profileId);
  };

  const handleAddCandidate = (category: ProfileGraphCategory, item: ApiProfileGraphAvailableItem) => {
    setDraft((current) => {
      if (!current) {
        return current;
      }
      const next = addProfileGraphSelection(current, category, item);
      const prefix = profileGraphNodePrefix(category);
      setSelectedNodeId(`${prefix}:${item.id}`);
      return next;
    });
  };

  const handleRemoveSelection = (category: ProfileGraphCategory, ref: string) => {
    setDraft((current) => current ? removeProfileGraphSelection(current, category, ref) : current);
    setSelectedNodeId(`profile:${selectedProfileId}`);
  };

  const handleApply = async () => {
    if (!selectedProfileId || !draft) {
      return;
    }
    setSaving(true);
    try {
      const response = await actions.apply(selectedProfileId, draft);
      setGraphData(response);
      setDraft(normalizeProfileGraphDocument(response.profile_id, response.graph));
      setProfiles((current) => current.map((profile) => (
        profile.profile_id === response.profile.profile_id ? response.profile : profile
      )));
      addToast('Profile graph saved.', 'success');
    } catch (applyError) {
      addToast(applyError instanceof Error ? applyError.message : 'Failed to save profile graph', 'error');
    } finally {
      setSaving(false);
    }
  };

  const handlePreview = async () => {
    if (!selectedProfileId || !draft) {
      return;
    }
    setPreviewing(true);
    try {
      const response = await actions.preview(selectedProfileId, draft);
      setPreview(response);
      addToast(response.compile_preview.ok ? 'Runtime preview ready.' : 'Runtime preview returned diagnostics.', response.compile_preview.ok ? 'success' : 'error');
    } catch (previewError) {
      addToast(previewError instanceof Error ? previewError.message : 'Failed to preview runtime selection', 'error');
    } finally {
      setPreviewing(false);
    }
  };

  const handleLaunch = async () => {
    if (!selectedProfileId) {
      return;
    }
    setLaunching(true);
    try {
      const response = await actions.launch(selectedProfileId);
      setActiveProfileId(response.active_profile_id || selectedProfileId);
      addToast('Startup profile launch requested.', 'success');
    } catch (launchError) {
      addToast(launchError instanceof Error ? launchError.message : 'Failed to launch startup profile', 'error');
    } finally {
      setLaunching(false);
    }
  };

  return (
    <ProfileGraphEditorShell
      profiles={profiles}
      activeProfileId={activeProfileId}
      selectedProfileId={selectedProfileId}
      graphData={graphData}
      draft={draft}
      preview={preview}
      activeCategory={activeCategory}
      paletteSearch={paletteSearch}
      selectedNodeId={selectedNodeId}
      loading={loading}
      graphLoading={graphLoading}
      saving={saving}
      previewing={previewing}
      launching={launching}
      error={error}
      onSelectProfile={handleSelectProfile}
      onCategoryChange={setActiveCategory}
      onPaletteSearchChange={setPaletteSearch}
      onAddCandidate={handleAddCandidate}
      onSelectNode={setSelectedNodeId}
      onRemoveSelection={handleRemoveSelection}
      onApply={handleApply}
      onPreview={handlePreview}
      onLaunch={handleLaunch}
    />
  );
}
