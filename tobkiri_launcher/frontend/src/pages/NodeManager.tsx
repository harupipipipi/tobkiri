import { useEffect, useMemo, useState } from 'react';
import {
  Boxes,
  CheckCircle2,
  ChevronRight,
  Globe2,
  Package,
  RefreshCw,
  Search,
  ShieldCheck,
  Unplug,
} from 'lucide-react';

import { Badge } from '@/src/components/ui/Badge';
import { Button } from '@/src/components/ui/Button';
import { InlineLoadError } from '@/src/components/ui/InlineLoadError';
import { TobkiriLoader, TobkiriLoadingMark } from '@/src/components/ui/TobkiriLoader';

function NodeManagerSkeleton() {
  return (
    <div className="flex flex-1 flex-col gap-5 overflow-hidden bg-bg-main p-6" role="status" aria-label="Loading capability access">
      <div className="h-8 w-56 animate-pulse rounded bg-bg-hover" />
      <div className="grid flex-1 gap-3 lg:grid-cols-[minmax(220px,280px)_1fr_minmax(280px,360px)]">
        {[0, 1, 2].map((item) => <div key={item} className="animate-pulse rounded-lg border border-border bg-bg-card" />)}
      </div>
    </div>
  );
}
import {
  approvePack,
  disableCapabilityProfileNode,
  enableCapabilityProfileNode,
  fetchCapabilityProfileNodes,
  fetchCapabilityProfiles,
  fetchStartupProfiles,
} from '@/src/lib/api';
import type {
  ApiCapabilityNode,
  ApiCapabilityProfile,
  ApiStartupProfile,
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

type CapabilityPackGroup = {
  packId: string;
  nodes: ApiCapabilityNode[];
  enabledCount: number;
  readyCount: number;
};

type CapabilityAccessCache = {
  startupProfiles: ApiStartupProfile[];
  capabilityProfiles: ApiCapabilityProfile[];
  capabilityProfileId: string;
  nodes: ApiCapabilityNode[];
  selectedPackId: string;
  selectedNodeId: string;
};

let capabilityAccessCache: CapabilityAccessCache | null = null;

function recordValue(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

function stringList(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter((item): item is string => typeof item === 'string' && item.trim().length > 0)
    : [];
}

export function capabilityPackId(node: ApiCapabilityNode): string {
  const fromMetadata = node.metadata?.pack_id;
  if (typeof fromMetadata === 'string' && fromMetadata.trim()) return fromMetadata;
  return node.node_id.split('.')[0] ?? 'unknown';
}

export function capabilityDomains(node: ApiCapabilityNode): string[] {
  const metadata = recordValue(node.metadata);
  const requirements = recordValue(node.requirements);
  const permissions = recordValue(node.permissions);
  const requirementNetwork = recordValue(requirements.network);
  const permissionNetwork = recordValue(permissions.network);
  return [...new Set([
    ...stringList(metadata.allowed_domains),
    ...stringList(metadata.network_domains),
    ...stringList(requirementNetwork.domains),
    ...stringList(requirementNetwork.allowed_domains),
    ...stringList(permissionNetwork.domains),
    ...stringList(permissionNetwork.allowed_domains),
  ])].sort();
}

export function capabilityProfileForStartup(
  startupProfile: ApiStartupProfile | null,
  capabilityProfiles: ApiCapabilityProfile[],
): string {
  const candidates = [
    startupProfile?.capability_profile_id,
    startupProfile?.default_graph,
    startupProfile?.graph_id,
  ].filter((value): value is string => Boolean(value));
  return candidates.find((candidate) => (
    capabilityProfiles.some((profile) => profile.profile_id === candidate)
  )) ?? capabilityProfiles[0]?.profile_id ?? '';
}

function buildPackGroups(nodes: ApiCapabilityNode[]): CapabilityPackGroup[] {
  const groups = new Map<string, ApiCapabilityNode[]>();
  nodes.forEach((node) => {
    const packId = capabilityPackId(node);
    groups.set(packId, [...(groups.get(packId) ?? []), node]);
  });
  return [...groups.entries()]
    .map(([packId, packNodes]) => ({
      packId,
      nodes: packNodes.sort((left, right) => capabilityNodeLabel(left).localeCompare(capabilityNodeLabel(right))),
      enabledCount: packNodes.filter((node) => node.state?.enabled).length,
      readyCount: packNodes.filter((node) => node.state?.status === 'ready').length,
    }))
    .sort((left, right) => left.packId.localeCompare(right.packId));
}

export function NodeManager() {
  const addToast = useAppStore((state) => state.addToast);
  const selectedStartupProfileId = useAppStore((state) => state.selectedStartupProfileId);
  const setSelectedStartupProfileId = useAppStore((state) => state.setSelectedStartupProfileId);
  const [startupProfiles, setStartupProfiles] = useState<ApiStartupProfile[]>(
    () => capabilityAccessCache?.startupProfiles ?? [],
  );
  const [capabilityProfiles, setCapabilityProfiles] = useState<ApiCapabilityProfile[]>(
    () => capabilityAccessCache?.capabilityProfiles ?? [],
  );
  const [capabilityProfileId, setCapabilityProfileId] = useState(
    () => capabilityAccessCache?.capabilityProfileId ?? '',
  );
  const [nodes, setNodes] = useState<ApiCapabilityNode[]>(
    () => capabilityAccessCache?.nodes ?? [],
  );
  const [selectedPackId, setSelectedPackId] = useState(
    () => capabilityAccessCache?.selectedPackId ?? '',
  );
  const [selectedNodeId, setSelectedNodeId] = useState(
    () => capabilityAccessCache?.selectedNodeId ?? '',
  );
  const [search, setSearch] = useState('');
  const [loading, setLoading] = useState(() => capabilityAccessCache === null);
  const [profileLoading, setProfileLoading] = useState(false);
  const [initialError, setInitialError] = useState<string | null>(null);
  const [profileError, setProfileError] = useState<string | null>(null);
  const [updating, setUpdating] = useState<string | null>(null);

  const selectedStartupProfile = startupProfiles.find(
    (profile) => profile.profile_id === selectedStartupProfileId,
  ) ?? null;

  const loadInitial = async () => {
    const hasCachedContent = startupProfiles.length > 0 && capabilityProfiles.length > 0;
    setLoading(!hasCachedContent);
    try {
      const [startupData, capabilityData] = await Promise.all([
        fetchStartupProfiles(),
        fetchCapabilityProfiles(),
      ]);
      setStartupProfiles(startupData.profiles);
      setCapabilityProfiles(capabilityData.profiles);
      const startupId = startupData.profiles.some(
        (profile) => profile.profile_id === selectedStartupProfileId,
      )
        ? selectedStartupProfileId
        : startupData.active_profile_id ?? startupData.profiles[0]?.profile_id ?? '';
      setSelectedStartupProfileId(startupId);
      const startupProfile = startupData.profiles.find((profile) => profile.profile_id === startupId) ?? null;
      setCapabilityProfileId(capabilityProfileForStartup(startupProfile, capabilityData.profiles));
      setInitialError(null);
    } catch (error) {
      if (!hasCachedContent) {
        setInitialError(error instanceof Error ? error.message : 'Failed to load capability access');
      }
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void loadInitial();
  }, []);

  useEffect(() => {
    if (!selectedStartupProfile || capabilityProfiles.length === 0) return;
    setCapabilityProfileId(capabilityProfileForStartup(selectedStartupProfile, capabilityProfiles));
  }, [capabilityProfiles, selectedStartupProfile]);

  const loadProfileNodes = async (profileId: string) => {
    if (!profileId) return;
    setProfileLoading(true);
    try {
      const response = await fetchCapabilityProfileNodes(profileId);
      const normalized = normalizeCapabilityProfileNodes(
        response,
        capabilityProfiles.find((profile) => profile.profile_id === profileId) ?? null,
      );
      setNodes(normalized.nodes);
      const groups = buildPackGroups(normalized.nodes);
      setSelectedPackId((current) => groups.some((group) => group.packId === current)
        ? current
        : groups[0]?.packId ?? '');
      setSelectedNodeId((current) => normalized.nodes.some((node) => node.node_id === current)
        ? current
        : normalized.nodes[0]?.node_id ?? '');
      setProfileError(null);
    } catch (error) {
      setProfileError(error instanceof Error ? error.message : 'Failed to load profile capability access');
    } finally {
      setProfileLoading(false);
    }
  };

  useEffect(() => {
    void loadProfileNodes(capabilityProfileId);
  }, [capabilityProfileId]);

  useEffect(() => {
    if (!startupProfiles.length || !capabilityProfiles.length || !capabilityProfileId) return;
    capabilityAccessCache = {
      startupProfiles,
      capabilityProfiles,
      capabilityProfileId,
      nodes,
      selectedPackId,
      selectedNodeId,
    };
  }, [startupProfiles, capabilityProfiles, capabilityProfileId, nodes, selectedPackId, selectedNodeId]);

  const packGroups = useMemo(() => buildPackGroups(nodes), [nodes]);
  const selectedPack = packGroups.find((group) => group.packId === selectedPackId) ?? null;
  const visibleNodes = useMemo(() => {
    const term = search.trim().toLowerCase();
    const packNodes = selectedPack?.nodes ?? [];
    if (!term) return packNodes;
    return packNodes.filter((node) => [
      node.node_id,
      capabilityNodeLabel(node),
      capabilityNodeDescription(node),
      ...capabilityDomains(node),
    ].join(' ').toLowerCase().includes(term));
  }, [search, selectedPack]);
  const selectedNode = nodes.find((node) => node.node_id === selectedNodeId)
    ?? visibleNodes[0]
    ?? null;

  const refreshProfile = async () => {
    await loadProfileNodes(capabilityProfileId);
  };

  const setNodeEnabled = async (node: ApiCapabilityNode, enabled: boolean) => {
    if (!capabilityProfileId) return;
    const previousNodes = nodes;
    setUpdating(node.node_id);
    setNodes((current) => current.map((candidate) => candidate.node_id === node.node_id
      ? {...candidate, state: {...candidate.state, enabled}}
      : candidate));
    try {
      if (enabled) {
        await enableCapabilityProfileNode(capabilityProfileId, node.node_id);
      } else {
        await disableCapabilityProfileNode(capabilityProfileId, node.node_id);
      }
      await refreshProfile();
      addToast(`${capabilityNodeLabel(node)} ${enabled ? 'enabled' : 'disabled'} for this profile.`, 'success');
    } catch (error) {
      setNodes(previousNodes);
      addToast(error instanceof Error ? error.message : 'Capability access could not be updated', 'error');
    } finally {
      setUpdating(null);
    }
  };

  const setPackEnabled = async (pack: CapabilityPackGroup, enabled: boolean) => {
    const changeableNodes = pack.nodes.filter((node) => node.node_id !== 'rumi.start');
    const changeableNodeIds = new Set(changeableNodes.map((node) => node.node_id));
    const previousNodes = nodes;
    setUpdating(`pack:${pack.packId}`);
    setNodes((current) => current.map((node) => changeableNodeIds.has(node.node_id)
      ? {...node, state: {...node.state, enabled}}
      : node));
    try {
      await Promise.all(changeableNodes.map((node) => (
        enabled
          ? enableCapabilityProfileNode(capabilityProfileId, node.node_id)
          : disableCapabilityProfileNode(capabilityProfileId, node.node_id)
      )));
      await refreshProfile();
      addToast(`${pack.packId} ${enabled ? 'enabled' : 'disabled'} for this profile.`, 'success');
    } catch (error) {
      setNodes(previousNodes);
      addToast(error instanceof Error ? error.message : 'Pack access could not be updated', 'error');
    } finally {
      setUpdating(null);
    }
  };

  const approveCapabilityPack = async (packId: string) => {
    setUpdating(`approve:${packId}`);
    try {
      await approvePack(packId);
      await refreshProfile();
      addToast(`${packId} approved.`, 'success');
    } catch (error) {
      addToast(error instanceof Error ? error.message : 'Pack approval failed', 'error');
    } finally {
      setUpdating(null);
    }
  };

  if (loading) return <NodeManagerSkeleton />;
  if (initialError) {
    return (
      <div className="flex flex-1 items-center justify-center bg-bg-main p-6">
        <InlineLoadError message={initialError} onRetry={() => void loadInitial()} title="Capability access could not load" />
      </div>
    );
  }

  const packFullyEnabled = Boolean(selectedPack?.nodes.length)
    && selectedPack?.nodes.every((node) => node.state?.enabled);

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-4 overflow-hidden bg-bg-main p-5">
      <header className="flex shrink-0 flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold text-text-main">Capability Access</h1>
          <p className="mt-1 text-xs text-text-muted">
            Choose a profile, then a Pack, then control each Node capability.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <select
            aria-label="Capability profile"
            className="rumi-select h-9 min-w-56 rounded-lg border border-border bg-bg-card px-3 pr-9 text-sm text-text-main"
            onChange={(event) => setSelectedStartupProfileId(event.target.value)}
            value={selectedStartupProfileId}
          >
            {startupProfiles.map((profile) => (
              <option key={profile.profile_id} value={profile.profile_id}>{profile.name}</option>
            ))}
          </select>
          <Button onClick={() => void refreshProfile()} size="sm" variant="outline">
            <RefreshCw className="h-4 w-4" /> Refresh
          </Button>
        </div>
      </header>

      {profileError ? (
        <InlineLoadError message={profileError} onRetry={() => void refreshProfile()} title="Profile access could not load" />
      ) : null}

      <div className="grid min-h-0 flex-1 gap-4 lg:grid-cols-[220px_minmax(340px,1fr)_320px]">
        <section className="min-h-0 overflow-y-auto rounded-xl border border-border bg-bg-card p-3">
          <div className="mb-3 flex items-center gap-2 px-1">
            <Package className="h-4 w-4 text-accent" />
            <h2 className="text-sm font-semibold text-text-main">Packs</h2>
          </div>
          <div className="space-y-1.5">
            {packGroups.map((pack) => (
              <button
                className={cn(
                  'w-full rounded-lg border px-3 py-2.5 text-left transition',
                  selectedPackId === pack.packId
                    ? 'border-accent bg-accent/10'
                    : 'border-transparent hover:border-border hover:bg-bg-hover',
                )}
                key={pack.packId}
                onClick={() => {
                  setSelectedPackId(pack.packId);
                  setSelectedNodeId(pack.nodes[0]?.node_id ?? '');
                }}
                type="button"
              >
                <div className="flex items-center justify-between gap-2">
                  <span className="truncate text-sm font-medium text-text-main">{pack.packId}</span>
                  <ChevronRight className="h-3.5 w-3.5 text-text-muted" />
                </div>
                <div className="mt-1 text-[11px] text-text-muted">
                  {pack.enabledCount}/{pack.nodes.length} enabled · {pack.readyCount} ready
                </div>
              </button>
            ))}
          </div>
        </section>

        <main className="flex min-h-0 min-w-0 flex-col rounded-xl border border-border bg-bg-card">
          <div className="flex shrink-0 flex-wrap items-center justify-between gap-3 border-b border-border p-4">
            <div>
              <div className="flex items-center gap-2">
                <Boxes className="h-4 w-4 text-accent" />
                <h2 className="text-sm font-semibold text-text-main">{selectedPackId || 'Select a Pack'}</h2>
                {profileLoading && nodes.length > 0 ? <TobkiriLoadingMark /> : null}
              </div>
              <p className="mt-1 text-xs text-text-muted">Nodes are execution parts owned by this Pack.</p>
            </div>
            {selectedPack ? (
              <Button
                loading={updating === `pack:${selectedPack.packId}`}
                onClick={() => void setPackEnabled(selectedPack, !packFullyEnabled)}
                size="sm"
                variant={packFullyEnabled ? 'outline' : 'default'}
              >
                {packFullyEnabled ? <Unplug className="h-4 w-4" /> : <CheckCircle2 className="h-4 w-4" />}
                {packFullyEnabled ? 'Disable Pack access' : 'Enable Pack access'}
              </Button>
            ) : null}
          </div>
          <div className="shrink-0 border-b border-border p-3">
            <label className="flex items-center gap-2 rounded-lg border border-border bg-bg-main px-3 py-2">
              <Search className="h-4 w-4 text-text-muted" />
              <input
                aria-label="Search capabilities"
                className="min-w-0 flex-1 bg-transparent text-sm text-text-main outline-none"
                onChange={(event) => setSearch(event.target.value)}
                placeholder="Search Nodes, capabilities, or domains"
                value={search}
              />
            </label>
          </div>
          <div className="min-h-0 flex-1 space-y-2 overflow-y-auto p-3">
            {profileLoading && nodes.length === 0 ? (
              <TobkiriLoader className="min-h-64" label="Loading profile capabilities" scope="inline" />
            ) : null}
            {visibleNodes.map((node) => {
              const domains = capabilityDomains(node);
              const enabled = node.state?.enabled === true;
              const approved = node.state?.approved !== false;
              return (
                <article
                  className={cn(
                    'rounded-xl border p-3 transition',
                    selectedNode?.node_id === node.node_id
                      ? 'border-accent bg-accent/5'
                      : 'border-border bg-bg-main hover:bg-bg-hover/40',
                  )}
                  key={node.node_id}
                  onClick={() => setSelectedNodeId(node.node_id)}
                >
                  <div className="flex items-start justify-between gap-3">
                    <button className="min-w-0 flex-1 text-left" onClick={() => setSelectedNodeId(node.node_id)} type="button">
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="text-sm font-semibold text-text-main">{capabilityNodeLabel(node)}</span>
                        <Badge variant={node.state?.status === 'ready' ? 'success' : 'secondary'}>{node.state?.status ?? 'unknown'}</Badge>
                      </div>
                      <div className="mt-1 truncate font-mono text-[11px] text-text-muted">{node.node_id}</div>
                      {domains.length ? (
                        <div className="mt-2 flex flex-wrap gap-1">
                          {domains.slice(0, 4).map((domain) => <Badge key={domain} variant="outline">{domain}</Badge>)}
                        </div>
                      ) : null}
                    </button>
                    {approved ? (
                      <button
                        aria-checked={enabled}
                        aria-label={`${enabled ? 'Disable' : 'Enable'} ${capabilityNodeLabel(node)}`}
                        className={cn(
                          'relative h-6 w-11 shrink-0 rounded-full transition',
                          enabled ? 'bg-accent' : 'bg-bg-hover ring-1 ring-border',
                        )}
                        disabled={updating !== null}
                        onClick={(event) => {
                          event.stopPropagation();
                          void setNodeEnabled(node, !enabled);
                        }}
                        role="switch"
                        type="button"
                      >
                        <span className={cn(
                          'absolute left-0 top-1 h-4 w-4 rounded-full bg-white shadow transition-transform',
                          enabled ? 'translate-x-6' : 'translate-x-1',
                        )} />
                      </button>
                    ) : (
                      <Button
                        loading={updating === `approve:${capabilityPackId(node)}`}
                        onClick={(event) => {
                          event.stopPropagation();
                          void approveCapabilityPack(capabilityPackId(node));
                        }}
                        size="sm"
                      >
                        <ShieldCheck className="h-3.5 w-3.5" /> Approve Pack
                      </Button>
                    )}
                  </div>
                </article>
              );
            })}
            {!profileLoading && visibleNodes.length === 0 ? (
              <div className="rounded-lg border border-dashed border-border px-4 py-10 text-center text-sm text-text-muted">
                No matching Nodes in this Pack.
              </div>
            ) : null}
          </div>
        </main>

        <aside className="min-h-0 overflow-y-auto rounded-xl border border-border bg-bg-card p-4">
          {selectedNode ? (
            <div className="space-y-5">
              <div>
                <div className="flex items-center justify-between gap-2">
                  <h2 className="text-sm font-semibold text-text-main">Node details</h2>
                  {updating === selectedNode.node_id ? <TobkiriLoadingMark /> : null}
                </div>
                <div className="mt-2 text-base font-semibold text-text-main">{capabilityNodeLabel(selectedNode)}</div>
                <p className="mt-1 text-xs leading-5 text-text-muted">
                  {capabilityNodeDescription(selectedNode) || 'No description declared.'}
                </p>
              </div>

              <section>
                <div className="mb-2 flex items-center gap-2">
                  <Globe2 className="h-4 w-4 text-accent" />
                  <h3 className="text-xs font-semibold uppercase tracking-wider text-text-muted">Connections</h3>
                </div>
                {capabilityDomains(selectedNode).length ? (
                  <div className="space-y-2">
                    {capabilityDomains(selectedNode).map((domain) => (
                      <div className="flex items-center justify-between gap-2 rounded-lg border border-border bg-bg-main px-3 py-2" key={domain}>
                        <span className="truncate font-mono text-xs text-text-main">{domain}</span>
                        <Badge variant={selectedNode.state?.enabled ? 'success' : 'secondary'}>
                          {selectedNode.state?.enabled ? 'Allowed by Node' : 'Blocked'}
                        </Badge>
                      </div>
                    ))}
                    <p className="text-[11px] leading-4 text-text-muted">
                      Domain access is bounded by this Node. Disable the Node to stop its declared connections for this profile.
                    </p>
                  </div>
                ) : (
                  <div className="rounded-lg border border-dashed border-border px-3 py-5 text-xs text-text-muted">
                    This Node does not declare network domains.
                  </div>
                )}
              </section>

              <section>
                <h3 className="mb-2 text-xs font-semibold uppercase tracking-wider text-text-muted">Capability ports</h3>
                <div className="space-y-2">
                  {capabilityNodePorts(selectedNode).map((port) => (
                    <div className="rounded-lg border border-border bg-bg-main px-3 py-2" key={port.id}>
                      <div className="text-xs font-medium text-text-main">{capabilityPortLabel(port)}</div>
                      <div className="mt-1 text-[11px] text-text-muted">
                        {capabilityPortStandards(port).join(', ') || 'No standard declared'}
                      </div>
                    </div>
                  ))}
                </div>
              </section>
            </div>
          ) : (
            <div className="flex min-h-64 items-center justify-center text-center text-sm text-text-muted">
              Select a Node to inspect its capability access.
            </div>
          )}
        </aside>
      </div>
    </div>
  );
}
