import {useEffect, useMemo, useState} from 'react';
import {useSearchParams} from 'react-router';
import {
  Boxes,
  Braces,
  GitBranch,
  Network,
  RefreshCw,
  Route,
  Search,
  ShieldCheck,
  Workflow,
  Zap,
} from 'lucide-react';

import {Badge} from '@/src/components/ui/Badge';
import {Button} from '@/src/components/ui/Button';
import {Input} from '@/src/components/ui/Input';
import {fetchApiMap, fetchStartupProfiles} from '@/src/lib/api';
import {
  API_MAP_NODE_CATEGORIES,
  API_MAP_NODE_CATEGORY_LABELS,
  apiMapNodeRoleDescription,
  apiMapNodeRoleLabel,
  countApiMapNodesByCategory,
  deriveApiMapView,
  edgePeerNodeId,
  formatApiMapEdgeKind,
  type ApiMapNodeCategory,
} from '@/src/lib/apiMap';
import type {
  ApiMapResponseData,
  ApiMapRuntimePath,
  ApiMapRuntimeStep,
  ApiMapRuntimeTarget,
  ApiProfileGraphEdge,
  ApiProfileGraphNode,
  ApiStartupProfile,
} from '@/src/lib/apiTypes';
import {cn} from '@/src/lib/utils';
import {useAppStore} from '@/src/store';

function resolveFocusedNodeId(nodes: ApiProfileGraphNode[], profileId: string, focus: string): string | null {
  const candidates = [
    String(focus || '').trim(),
    profileId ? `profile:${profileId}` : '',
    'api:POST /api/chat/conversations/{id}/messages',
    nodes[0]?.id || '',
  ].filter(Boolean);
  return candidates.find((candidate) => nodes.some((node) => node.id === candidate)) || null;
}

export function ApiMap() {
  const addToast = useAppStore((state) => state.addToast);
  const advancedProfileId = useAppStore((state) => state.selectedStartupProfileId);
  const setAdvancedProfileId = useAppStore((state) => state.setSelectedStartupProfileId);
  const [searchParams, setSearchParams] = useSearchParams();
  const [profiles, setProfiles] = useState<ApiStartupProfile[]>([]);
  const [profileId, setProfileId] = useState(searchParams.get('profile_id') || '');
  const [focusInput, setFocusInput] = useState(searchParams.get('focus') || '');
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(searchParams.get('focus'));
  const [nodeSearch, setNodeSearch] = useState('');
  const [nodeCategory, setNodeCategory] = useState<ApiMapNodeCategory>('all');
  const [data, setData] = useState<ApiMapResponseData | null>(null);
  const [loading, setLoading] = useState(true);

  const syncQueryParams = (nextProfileId: string, nextFocus: string) => {
    const next = new URLSearchParams(searchParams);
    if (nextProfileId) {
      next.set('profile_id', nextProfileId);
    } else {
      next.delete('profile_id');
    }
    if (nextFocus.trim()) {
      next.set('focus', nextFocus.trim());
    } else {
      next.delete('focus');
    }
    setSearchParams(next, {replace: true});
  };

  const loadMap = async (nextProfileId: string, nextFocus: string) => {
    setLoading(true);
    try {
      const response = await fetchApiMap({profile_id: nextProfileId || undefined});
      setData(response);
      setSelectedNodeId(resolveFocusedNodeId(response.nodes, nextProfileId, nextFocus));
    } catch (error) {
      addToast(error instanceof Error ? error.message : 'Failed to load API map', 'error');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    let cancelled = false;
    const requestedProfileId = searchParams.get('profile_id') || '';
    const requestedFocus = searchParams.get('focus') || '';
    setProfileId(requestedProfileId);
    setFocusInput(requestedFocus);
    setLoading(true);

    Promise.all([
      fetchStartupProfiles(),
      fetchApiMap({profile_id: requestedProfileId || undefined}),
    ])
      .then(([startupProfiles, apiMap]) => {
        if (cancelled) return;
        const resolvedProfileId = requestedProfileId
          || advancedProfileId
          || startupProfiles.active_profile_id
          || startupProfiles.profiles[0]?.profile_id
          || '';
        setProfiles(startupProfiles.profiles);
        setProfileId(resolvedProfileId);
        if (resolvedProfileId) setAdvancedProfileId(resolvedProfileId);
        setData(apiMap);
        setSelectedNodeId(resolveFocusedNodeId(apiMap.nodes, resolvedProfileId, requestedFocus));
      })
      .catch((error) => {
        if (!cancelled) {
          addToast(error instanceof Error ? error.message : 'Failed to load API map', 'error');
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [addToast, searchParams]);

  const categoryCounts = useMemo(() => countApiMapNodesByCategory(data?.nodes || []), [data]);
  const visibleCategories = useMemo(
    () => API_MAP_NODE_CATEGORIES.filter((category) => category === 'all' || categoryCounts[category] > 0),
    [categoryCounts],
  );
  const view = useMemo(() => deriveApiMapView(data, {
    selectedNodeId,
    search: nodeSearch,
    category: nodeCategory,
  }), [data, nodeCategory, nodeSearch, selectedNodeId]);

  const selectedNode = view.selectedNode;
  const selectedRuntimePath = view.selectedRuntimePath;
  const connectionItems = useMemo(() => {
    if (!selectedNode) return [];
    return [
      ...view.outboundEdges.map((edge) => ({edge, direction: 'outbound' as const})),
      ...view.inboundEdges.map((edge) => ({edge, direction: 'inbound' as const})),
    ]
      .map((item) => {
        const peerNodeId = edgePeerNodeId(item.edge, selectedNode.id);
        return {...item, node: peerNodeId ? view.nodeById.get(peerNodeId) || null : null};
      })
      .filter((item): item is {edge: ApiProfileGraphEdge; direction: 'inbound' | 'outbound'; node: ApiProfileGraphNode} => Boolean(item.node))
      .sort((left, right) => formatApiMapEdgeKind(left.edge.kind).localeCompare(formatApiMapEdgeKind(right.edge.kind)));
  }, [selectedNode, view.inboundEdges, view.nodeById, view.outboundEdges]);

  const profileRuntime = asRecord(data?.profile_runtime);
  const profilePolicy = asRecord(profileRuntime.policy);
  const selectedProfileRuntime = asRecord(profileRuntime.selected);

  const handleApplyContext = () => {
    setAdvancedProfileId(profileId);
    syncQueryParams(profileId, focusInput);
  };

  const handleRefresh = () => {
    void loadMap(profileId, focusInput);
  };

  const handleSelectNode = (nodeId: string) => {
    setSelectedNodeId(nodeId);
    setFocusInput(nodeId);
    syncQueryParams(profileId, nodeId);
  };

  const handleResetFocus = () => {
    const nextFocus = profileId ? `profile:${profileId}` : 'api:POST /api/chat/conversations/{id}/messages';
    setFocusInput(nextFocus);
    setSelectedNodeId(nextFocus);
    syncQueryParams(profileId, nextFocus);
  };

  return (
    <div className="flex flex-1 flex-col gap-5 overflow-y-auto overflow-x-hidden bg-bg-main p-6 animate-in fade-in slide-in-from-bottom-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold text-text-main">API Map</h1>
          <div className="mt-2 flex flex-wrap gap-2">
            <Badge variant="outline">{data?.summary.route_count || 0} routes</Badge>
            <Badge variant="outline">{data?.summary.flow_count || 0} flows</Badge>
            <Badge variant="outline">{categoryCounts.operation || 0} runtime units</Badge>
            <Badge variant="outline">{data?.summary.tool_count || 0} tool facades</Badge>
          </div>
          <p className="mt-2 max-w-3xl text-xs text-text-muted">
            Routes and flows converge on defaultspack operations. Tools are model-facing facades; blocks stay as implementation detail.
          </p>
        </div>
        <Button type="button" size="sm" variant="outline" onClick={handleRefresh}>
          <RefreshCw className="h-4 w-4" />
          Refresh
        </Button>
      </div>

      <section className="rounded-xl bg-bg-card/70 p-3 ring-1 ring-border/70">
        <div className="grid gap-2 lg:grid-cols-[minmax(180px,220px)_minmax(240px,1fr)_auto]">
          <label className="text-sm text-text-muted">
            <span className="sr-only">Profile</span>
            <select
              className="h-9 w-full rounded-lg border border-border bg-bg-main px-3 text-sm text-text-main"
              value={profileId}
              onChange={(event) => setProfileId(event.target.value)}
            >
              <option value="">Active profile</option>
              {profiles.map((profile) => (
                <option key={profile.profile_id} value={profile.profile_id}>{profile.name}</option>
              ))}
            </select>
          </label>
          <label className="text-sm text-text-muted">
            <span className="sr-only">Focus</span>
            <Input
              className="h-9 font-mono text-xs"
              placeholder="api:POST /api/chat/conversations/{id}/messages"
              value={focusInput}
              onChange={(event) => setFocusInput(event.target.value)}
            />
          </label>
          <div className="flex flex-wrap items-end gap-2">
            <Button type="button" size="sm" onClick={handleApplyContext}>
              <Route className="h-4 w-4" />
              Apply
            </Button>
            <Button type="button" size="sm" variant="outline" onClick={handleResetFocus}>
              Reset
            </Button>
          </div>
        </div>
      </section>

      <div className="grid gap-4 lg:grid-cols-[280px_minmax(0,1fr)] 2xl:grid-cols-[300px_minmax(0,1fr)]">
        <section className="min-w-0 space-y-3 lg:sticky lg:top-4 lg:self-start">
          <PanelTitle icon={Search} title="Explore" />
          <div className="rounded-xl border border-border/80 bg-bg-card/80 p-3">
            <Input
              className="h-9 text-sm"
              placeholder="Search runtime"
              value={nodeSearch}
              onChange={(event) => setNodeSearch(event.target.value)}
            />
            <div className="mt-3 flex flex-wrap gap-1.5">
              {visibleCategories.map((category) => (
                <button
                  key={category}
                  type="button"
                  className={cn(
                    'inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-[11px] transition-colors',
                    nodeCategory === category
                      ? 'border-accent bg-accent/10 text-accent'
                      : 'border-border text-text-muted hover:bg-bg-hover hover:text-text-main',
                  )}
                  onClick={() => setNodeCategory(category)}
                >
                  <span>{API_MAP_NODE_CATEGORY_LABELS[category]}</span>
                  <span className="rounded-full bg-bg-main px-1.5 py-0.5 text-[10px] text-text-muted">
                    {categoryCounts[category]}
                  </span>
                </button>
              ))}
            </div>
            <div className="mt-3 max-h-[calc(100vh-330px)] min-h-[260px] space-y-1.5 overflow-auto pr-1">
              {view.listNodes.slice(0, 60).map((node) => (
                <button
                  key={node.id}
                  type="button"
                  className={cn(
                    'w-full min-w-0 rounded-lg border px-2.5 py-2 text-left transition-colors',
                    selectedNode?.id === node.id ? 'border-accent bg-accent/10' : 'border-border hover:bg-bg-hover',
                  )}
                  onClick={() => handleSelectNode(node.id)}
                >
                  <div className="flex flex-wrap items-center gap-2">
                    <Badge variant={selectedNode?.id === node.id ? 'default' : 'outline'}>
                      {apiMapNodeRoleLabel(node)}
                    </Badge>
                    {node.metadata?.risk ? <Badge variant="warning">{String(node.metadata.risk)}</Badge> : null}
                  </div>
                  <div className="mt-1.5 truncate text-sm font-semibold text-text-main">{node.label || node.ref || node.id}</div>
                  <div className="truncate font-mono text-[11px] text-text-muted">{node.id}</div>
                </button>
              ))}
              {!view.listNodes.length ? <EmptyBox text="No matching runtime entities." /> : null}
            </div>
          </div>
        </section>

        <main className="min-w-0 space-y-4">
          <div className="grid gap-4 2xl:grid-cols-[minmax(0,1fr)_340px]">
            <div className="min-w-0 space-y-4">
              <RuntimeTrace
                loading={loading}
                path={selectedRuntimePath}
                selectedNode={selectedNode}
                onSelectNode={handleSelectNode}
              />

              <ConnectionsPanel
                groups={view.connectionGroups}
                items={connectionItems}
                onSelectNode={handleSelectNode}
              />
            </div>

            <aside className="min-w-0 space-y-4">
              <ProfileRuntimePanel
                profileRuntime={profileRuntime}
                policy={profilePolicy}
                selected={selectedProfileRuntime}
              />
              <InspectorPanel selectedNode={selectedNode} />
            </aside>
          </div>
        </main>
      </div>

      <section className="rounded-xl border border-border bg-bg-card p-4">
        <PanelTitle icon={ShieldCheck} title="Diagnostics" />
        <div className="mt-3 space-y-2">
          {data?.diagnostics?.length ? data.diagnostics.map((diagnostic, index) => (
            <div key={`${diagnostic.code}-${index}`} className="rounded-lg border border-border bg-bg-main/70 px-3 py-2 text-sm text-text-muted">
              <div className="font-medium text-text-main">{diagnostic.code}</div>
              <div className="mt-1 text-xs">{diagnostic.message}</div>
            </div>
          )) : <EmptyBox text="No diagnostics for this profile and focus." />}
        </div>
      </section>
    </div>
  );
}

function RuntimeTrace({
  loading,
  path,
  selectedNode,
  onSelectNode,
}: {
  loading: boolean;
  path: ApiMapRuntimePath | null;
  selectedNode: ApiProfileGraphNode | null;
  onSelectNode: (nodeId: string) => void;
}) {
  const [showAllSteps, setShowAllSteps] = useState(false);

  if (loading) {
    return (
      <div className="rounded-xl border border-border bg-bg-card p-4">
        <PanelTitle icon={Workflow} title="Runtime Trace" compact />
        <div className="mt-3">
          <EmptyBox text="Loading runtime map..." />
        </div>
      </div>
    );
  }

  if (!path) {
    return (
      <div className="rounded-xl border border-border bg-bg-card p-4">
        <PanelTitle icon={Workflow} title="Runtime Trace" compact />
        <div className="mt-3">
          <EmptyBox text={selectedNode ? 'This entity is not part of an HTTP runtime trace.' : 'Select a runtime entity.'} />
        </div>
      </div>
    );
  }

  const steps = path.steps || [];
  const visibleSteps = showAllSteps ? steps : steps.slice(0, 6);
  const hiddenStepCount = Math.max(0, steps.length - visibleSteps.length);

  return (
    <div className="rounded-xl border border-border bg-bg-card p-4 shadow-[0_24px_80px_-70px_rgba(0,0,0,0.9)]">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <PanelTitle icon={Workflow} title="Runtime Trace" compact />
        <div className="flex flex-wrap gap-2">
          <Badge variant="default">{path.entrypoint.method || 'HTTP'}</Badge>
          <Badge variant="outline">{path.entrypoint.source || 'route_registry'}</Badge>
          <Badge variant="outline">{steps.length} steps</Badge>
        </div>
      </div>

      <div className="mt-4 grid gap-3">
        <TraceCard
          icon={Route}
          title={path.label}
          subtitle={path.entrypoint.path || path.entrypoint.node_id}
          badge="HTTP route"
          onClick={() => onSelectNode(path.entrypoint.node_id)}
        />
        {path.primary ? (
          <TargetTraceCard target={path.primary} badge="primary" onSelectNode={onSelectNode} />
        ) : null}
        {steps.length ? (
          <div className="rounded-lg border border-border bg-bg-main/60 p-3">
            <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
              <div className="flex items-center gap-2 text-sm font-semibold text-text-main">
                <GitBranch className="h-4 w-4 text-accent" />
                Flow steps
              </div>
              {hiddenStepCount ? (
                <Button type="button" size="sm" variant="ghost" onClick={() => setShowAllSteps(true)}>
                  Show {hiddenStepCount} more
                </Button>
              ) : showAllSteps && steps.length > 6 ? (
                <Button type="button" size="sm" variant="ghost" onClick={() => setShowAllSteps(false)}>
                  Collapse
                </Button>
              ) : null}
            </div>
            <div className="grid gap-2">
              {visibleSteps.map((step) => (
                <StepTraceCard key={step.node_id} step={step} onSelectNode={onSelectNode} />
              ))}
            </div>
          </div>
        ) : null}
        {path.fallback ? (
          <TargetTraceCard target={path.fallback} badge="fallback" onSelectNode={onSelectNode} />
        ) : null}
      </div>
    </div>
  );
}

function StepTraceCard({step, onSelectNode}: {step: ApiMapRuntimeStep; onSelectNode: (nodeId: string) => void}) {
  return (
    <button
      type="button"
      className="grid min-w-0 gap-2 rounded-lg border border-border bg-bg-card px-3 py-2 text-left transition-colors hover:bg-bg-hover sm:grid-cols-[150px_minmax(0,1fr)]"
      onClick={() => onSelectNode(step.node_id)}
    >
      <div className="flex items-center gap-2">
        <Badge variant="secondary">#{step.order || '-'}</Badge>
        <Badge variant="outline">{step.step_type === 'function' ? 'operation' : step.step_type || 'step'}</Badge>
      </div>
      <div className="min-w-0">
        <div className="truncate text-sm font-semibold text-text-main">{step.id}</div>
        <div className="truncate font-mono text-[11px] text-text-muted">
          {step.target?.id || step.target?.node_id || step.node_id}
        </div>
      </div>
    </button>
  );
}

function TargetTraceCard({
  target,
  badge,
  onSelectNode,
}: {
  target: ApiMapRuntimeTarget;
  badge: string;
  onSelectNode: (nodeId: string) => void;
}) {
  const nodeId = target.node_id || target.block_node_id || '';
  const targetBadge = target.kind === 'function' ? `${badge} operation` : target.kind === 'block' ? `${badge} implementation` : badge;
  return (
    <TraceCard
      icon={target.kind === 'flow' ? Workflow : target.kind === 'function' ? Zap : Braces}
      title={target.id || target.block_module || nodeId}
      subtitle={target.block_module || nodeId}
      badge={targetBadge}
      onClick={nodeId ? () => onSelectNode(nodeId) : undefined}
    />
  );
}

function TraceCard({
  icon: Icon,
  title,
  subtitle,
  badge,
  onClick,
}: {
  icon: typeof Route;
  title?: string;
  subtitle?: string;
  badge: string;
  onClick?: () => void;
}) {
  const Comp = onClick ? 'button' : 'div';
  return (
    <Comp
      type={onClick ? 'button' : undefined}
      className="flex w-full min-w-0 items-start gap-3 rounded-lg border border-border bg-bg-main/70 px-3 py-3 text-left transition-colors hover:bg-bg-hover"
      onClick={onClick}
    >
      <div className="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-lg border border-border bg-bg-card text-accent">
        <Icon className="h-4 w-4" />
      </div>
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-2">
          <Badge variant="outline">{badge}</Badge>
        </div>
        <div className="mt-2 truncate text-sm font-semibold text-text-main">{title || 'unresolved'}</div>
        {subtitle ? <div className="truncate font-mono text-[11px] text-text-muted">{subtitle}</div> : null}
      </div>
    </Comp>
  );
}

function ProfileRuntimePanel({
  profileRuntime,
  policy,
  selected,
}: {
  profileRuntime: Record<string, unknown>;
  policy: Record<string, unknown>;
  selected: Record<string, unknown>;
}) {
  const enforce = Boolean(policy.enforce_api_route_allowlist);
  return (
    <div className="rounded-xl border border-border bg-bg-card p-4">
      <PanelTitle icon={ShieldCheck} title="Profile Runtime" compact />
      <div className="mt-3 space-y-3">
        <KeyValue label="Profile" value={String(profileRuntime.profile_id || 'active')} />
        <KeyValue label="Rule" value={String(profileRuntime.system_prompt_id || profileRuntime.default_prompt_id || 'none')} />
        <div className="flex flex-wrap gap-2">
          <Badge variant={enforce ? 'success' : 'warning'}>
            API strict {enforce ? 'on' : 'off'}
          </Badge>
          <Badge variant="outline">{listLength(policy.tool_allowlist)} allowed tools</Badge>
          <Badge variant="outline">{listLength(policy.api_route_allowlist)} selected routes</Badge>
        </div>
        <div className="grid grid-cols-2 gap-2">
          <MiniCount label="Tools" value={listLength(selected.tools)} />
          <MiniCount label="Webhooks" value={listLength(selected.webhooks)} />
          <MiniCount label="Rules" value={listLength(selected.prompts)} />
          <MiniCount label="Frontend" value={listLength(selected.frontend)} />
        </div>
      </div>
    </div>
  );
}

function InspectorPanel({selectedNode}: {selectedNode: ApiProfileGraphNode | null}) {
  const description = selectedNode ? apiMapNodeRoleDescription(selectedNode) : '';
  return (
    <div className="rounded-xl border border-border bg-bg-card p-4">
      <PanelTitle icon={Boxes} title="Inspector" compact />
      {selectedNode ? (
        <div className="mt-3 space-y-3">
          <div className="flex flex-wrap gap-2">
            <Badge variant="default">{apiMapNodeRoleLabel(selectedNode)}</Badge>
            <Badge variant="outline">{selectedNode.kind}</Badge>
          </div>
          {description ? (
            <div className="rounded-lg border border-border bg-bg-main/70 px-3 py-2 text-xs text-text-muted">
              {description}
            </div>
          ) : null}
          <div>
            <div className="text-sm font-semibold text-text-main">{selectedNode.label || selectedNode.id}</div>
            <div className="mt-1 break-all rounded-lg border border-border bg-bg-main/70 px-3 py-2 font-mono text-[11px] text-text-muted">
              {selectedNode.id}
            </div>
          </div>
          <details className="rounded-lg border border-border bg-bg-main/70">
            <summary className="cursor-pointer px-3 py-2 text-xs font-medium text-text-muted">
              Raw metadata
            </summary>
            <pre className="max-h-[220px] overflow-auto border-t border-border p-3 text-xs text-text-muted">
              {JSON.stringify(selectedNode.metadata || {}, null, 2)}
            </pre>
          </details>
        </div>
      ) : (
        <div className="mt-3">
          <EmptyBox text="No runtime entity selected." />
        </div>
      )}
    </div>
  );
}

function ConnectionsPanel({
  groups,
  items,
  onSelectNode,
}: {
  groups: Array<{kind: string; edges: ApiProfileGraphEdge[]}>;
  items: Array<{edge: ApiProfileGraphEdge; direction: 'inbound' | 'outbound'; node: ApiProfileGraphNode}>;
  onSelectNode: (nodeId: string) => void;
}) {
  return (
    <div className="rounded-xl border border-border bg-bg-card p-4">
      <PanelTitle icon={Network} title="Connections" compact />
      <div className="mt-3 flex flex-wrap gap-2">
        {groups.map((group) => (
          <Badge key={group.kind} variant="secondary">
            {formatApiMapEdgeKind(group.kind)} {group.edges.length}
          </Badge>
        ))}
      </div>
      <div className="mt-3 max-h-[360px] space-y-2 overflow-auto pr-1">
        {items.map((item, index) => (
          <button
            key={`${item.edge.id}-${index}`}
            type="button"
            className="w-full rounded-lg border border-border bg-bg-main/70 px-3 py-3 text-left transition-colors hover:bg-bg-hover"
            onClick={() => onSelectNode(item.node.id)}
          >
            <div className="flex items-center justify-between gap-2">
              <Badge variant={item.direction === 'outbound' ? 'default' : 'secondary'}>
                {item.direction}
              </Badge>
              <span className="text-[11px] uppercase tracking-[0.14em] text-text-muted">
                {formatApiMapEdgeKind(item.edge.kind)}
              </span>
            </div>
            <div className="mt-2 truncate text-sm font-semibold text-text-main">{item.node.label || item.node.id}</div>
            <div className="truncate font-mono text-[11px] text-text-muted">{item.node.id}</div>
          </button>
        ))}
        {!items.length ? <EmptyBox text="No direct connections." /> : null}
      </div>
    </div>
  );
}

function PanelTitle({icon: Icon, title, compact = false}: {icon: typeof Route; title: string; compact?: boolean}) {
  return (
    <div className={cn('flex items-center gap-2', !compact && 'mb-3')}>
      <Icon className="h-4 w-4 text-accent" />
      <h2 className="text-sm font-semibold text-text-main">{title}</h2>
    </div>
  );
}

function EmptyBox({text}: {text: string}) {
  return (
    <div className="rounded-lg border border-dashed border-border px-3 py-6 text-sm text-text-muted">
      {text}
    </div>
  );
}

function KeyValue({label, value}: {label: string; value: string}) {
  return (
    <div>
      <div className="text-[11px] uppercase tracking-[0.14em] text-text-muted">{label}</div>
      <div className="mt-1 truncate text-sm font-semibold text-text-main">{value}</div>
    </div>
  );
}

function MiniCount({label, value}: {label: string; value: number}) {
  return (
    <div className="rounded-lg border border-border bg-bg-main/70 px-3 py-2">
      <div className="text-[10px] uppercase tracking-[0.14em] text-text-muted">{label}</div>
      <div className="mt-1 text-lg font-semibold text-text-main">{value}</div>
    </div>
  );
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

function listLength(value: unknown): number {
  return Array.isArray(value) ? value.length : 0;
}
