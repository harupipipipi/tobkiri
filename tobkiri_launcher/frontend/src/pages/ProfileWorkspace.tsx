import { useEffect, useMemo, useState } from 'react';
import { Database, FileCode2, Files, FolderOpen, RefreshCw, ShieldCheck } from 'lucide-react';

import {Badge} from '@/src/components/ui/Badge';
import {Button} from '@/src/components/ui/Button';
import {InlineLoadError} from '@/src/components/ui/InlineLoadError';
import type {ApiProfileWorkspaceDetail, StartupProfilesResponseData} from '@/src/lib/apiTypes';
import {fetchActiveProfileWorkspace, fetchProfileWorkspace} from '@/src/lib/profileWorkspaceApi';
import {cn} from '@/src/lib/utils';
import {useAppStore} from '@/src/store';
import {FlowViewer} from './FlowViewer';

type WorkspaceTab = 'overview' | 'configuration' | 'resources' | 'flow';

const TABS: Array<{id: WorkspaceTab; label: string; icon: typeof FolderOpen}> = [
  {id: 'overview', label: 'Overview', icon: FolderOpen},
  {id: 'configuration', label: 'Configuration', icon: Database},
  {id: 'resources', label: 'Resources', icon: Files},
  {id: 'flow', label: 'Flow YAML', icon: FileCode2},
];

function JsonBlock({value}: {value: unknown}) {
  return (
    <pre className="h-full min-h-0 overflow-auto rounded-lg border border-border bg-bg-main p-3 font-mono text-xs leading-5 text-text-main">
      {JSON.stringify(value, null, 2)}
    </pre>
  );
}

function PathRow({label, value}: {label: string; value?: string}) {
  return (
    <div className="grid min-w-0 gap-1 border-b border-border/70 py-2.5 last:border-b-0 sm:grid-cols-[150px_minmax(0,1fr)]">
      <div className="text-[11px] font-medium uppercase tracking-[0.12em] text-text-muted">{label}</div>
      <div className="min-w-0 break-all font-mono text-xs text-text-main">{value || '--'}</div>
    </div>
  );
}

export function ProfileWorkspace() {
  const addToast = useAppStore((state) => state.addToast);
  const runtimeReady = useAppStore((state) => state.runtimeReady);
  const runtimeStatus = useAppStore((state) => state.runtimeStatus);
  const runtimeError = useAppStore((state) => state.runtimeError);
  const refreshRuntimeHealth = useAppStore((state) => state.refreshRuntimeHealth);
  const [startupProfiles, setStartupProfiles] = useState<StartupProfilesResponseData | null>(null);
  const [workspace, setWorkspace] = useState<ApiProfileWorkspaceDetail | null>(null);
  const [selectedProfileId, setSelectedProfileId] = useState<string>('');
  const [activeTab, setActiveTab] = useState<WorkspaceTab>('overview');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const loadWorkspace = async (profileId?: string) => {
    setLoading(true);
    try {
      if (profileId) {
        const nextWorkspace = await fetchProfileWorkspace(profileId);
        setWorkspace(nextWorkspace);
        setSelectedProfileId(profileId);
      } else {
        const response = await fetchActiveProfileWorkspace();
        setStartupProfiles(response.startupProfiles);
        setWorkspace(response.workspace);
        setSelectedProfileId(response.activeProfile?.profile_id ?? '');
      }
      setError(null);
    } catch (loadError) {
      const message = loadError instanceof Error ? loadError.message : 'Failed to load profile workspace';
      setError(message);
      addToast(message, 'error');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    if (runtimeReady) void loadWorkspace();
  }, [runtimeReady]);

  const profiles = startupProfiles?.profiles ?? [];
  const manifestItems = useMemo(() => {
    const items = workspace?.resource_snapshot_manifest?.items;
    return Array.isArray(items) ? items as Array<Record<string, unknown>> : [];
  }, [workspace]);

  return (
    <div className="flex h-full min-h-0 flex-col overflow-hidden bg-bg-main">
      <header className="shrink-0 border-b border-border bg-bg-card px-5 py-3">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="min-w-0">
            <h1 className="text-lg font-semibold text-text-main">Profile Files</h1>
            <div className="truncate text-xs text-text-muted">
              Inspect generated files and runtime configuration for {workspace?.profile.name ?? 'the active profile'}.
            </div>
          </div>
          <div className="flex items-center gap-2">
            <select
              className="rumi-select h-9 min-w-48 rounded-md border border-border px-3 pr-9 text-sm"
              value={selectedProfileId}
              onChange={(event) => void loadWorkspace(event.target.value)}
              aria-label="Profile"
            >
              {profiles.map((profile) => (
                <option key={profile.profile_id} value={profile.profile_id}>{profile.name}</option>
              ))}
            </select>
            <Button variant="secondary" size="sm" onClick={() => void loadWorkspace(selectedProfileId)} loading={loading}>
              <RefreshCw className="h-4 w-4" /> Refresh
            </Button>
          </div>
        </div>
      </header>

      <nav className="flex shrink-0 gap-1 border-b border-border bg-bg-card px-5" aria-label="Profile file sections">
        {TABS.map((tab) => {
          const Icon = tab.icon;
          return (
            <button
              key={tab.id}
              type="button"
              onClick={() => setActiveTab(tab.id)}
              className={cn(
                'flex items-center gap-2 border-b-2 px-3 py-2.5 text-xs font-medium transition-colors',
                activeTab === tab.id
                  ? 'border-accent text-text-main'
                  : 'border-transparent text-text-muted hover:text-text-main',
              )}
            >
              <Icon className="h-3.5 w-3.5" /> {tab.label}
            </button>
          );
        })}
      </nav>

      <main className="min-h-0 flex-1 overflow-hidden p-5">
        {!runtimeReady ? (
          <InlineLoadError
            title={runtimeStatus === 'error' ? 'Runtime failed to start' : 'Runtime is not ready'}
            message={runtimeError || 'Profile workspace data becomes available after the local runtime is ready.'}
            onRetry={() => void refreshRuntimeHealth()}
          />
        ) : null}
        {loading && !workspace ? <div className="text-sm text-text-muted">Loading profile files...</div> : null}
        {error && runtimeReady ? (
          <InlineLoadError
            title="Profile workspace could not be loaded"
            message={error}
            onRetry={() => void loadWorkspace(selectedProfileId || undefined)}
            retrying={loading}
            stale={Boolean(workspace)}
          />
        ) : null}
        {workspace && activeTab === 'overview' ? (
          <div className="grid h-full min-h-0 gap-4 overflow-auto xl:grid-cols-[minmax(0,1.35fr)_minmax(280px,0.65fr)]">
            <section className="rounded-xl border border-border bg-bg-card p-4">
              <div className="mb-2 flex items-center gap-2"><FolderOpen className="h-4 w-4 text-accent" /><h2 className="text-sm font-semibold text-text-main">Workspace paths</h2><Badge variant="outline">{workspace.profile.profile_id}</Badge></div>
              <PathRow label="Workspace" value={workspace.profile_workspace.root} />
              <PathRow label="Profile file" value={workspace.profile_workspace.profile_file} />
              <PathRow label="Database" value={workspace.profile_workspace.database_path} />
              <PathRow label="User data" value={workspace.profile_workspace.user_data_dir} />
              <PathRow label="Startup" value={workspace.profile_workspace.startup_dir} />
              <PathRow label="Flows" value={workspace.profile_workspace.flows_dir} />
              <PathRow label="Prompts" value={workspace.profile_workspace.prompts_dir} />
              <PathRow label="Permissions" value={workspace.profile_workspace.permissions_dir} />
            </section>
            <section className="min-h-0 overflow-auto rounded-xl border border-border bg-bg-card p-4">
              <div className="mb-3 flex items-center gap-2"><ShieldCheck className="h-4 w-4 text-accent" /><h2 className="text-sm font-semibold text-text-main">Permission files</h2></div>
              <div className="divide-y divide-border rounded-lg border border-border">
                {Object.entries(workspace.permissions).map(([name, status]) => (
                  <div key={name} className="flex items-center justify-between gap-3 px-3 py-2.5">
                    <div className="min-w-0"><div className="truncate font-mono text-xs text-text-main">{name}</div><div className="truncate text-[11px] text-text-muted">{status.path}</div></div>
                    <Badge variant={status.exists ? 'success' : 'warning'}>{status.exists ? 'present' : 'missing'}</Badge>
                  </div>
                ))}
              </div>
            </section>
          </div>
        ) : null}
        {workspace && activeTab === 'configuration' ? (
          <div className="grid h-full min-h-0 gap-4 xl:grid-cols-2">
            <section className="flex min-h-0 flex-col gap-3"><div className="flex items-center gap-2"><Database className="h-4 w-4 text-accent" /><h2 className="text-sm font-semibold text-text-main">Startup configuration</h2></div><div className="min-h-0 flex-1"><JsonBlock value={workspace.startup_config} /></div></section>
            <section className="flex min-h-0 flex-col gap-3"><div className="flex items-center justify-between"><h2 className="text-sm font-semibold text-text-main">Resource snapshot</h2><Badge variant={manifestItems.length ? 'success' : 'warning'}>{manifestItems.length} items</Badge></div><div className="min-h-0 flex-1"><JsonBlock value={workspace.resource_snapshot_manifest} /></div></section>
          </div>
        ) : null}
        {workspace && activeTab === 'resources' ? (
          <div className="grid h-full min-h-0 gap-4 xl:grid-cols-2">
            <section className="flex min-h-0 flex-col gap-3"><h2 className="text-sm font-semibold text-text-main">Profile flows</h2><div className="min-h-0 flex-1"><JsonBlock value={workspace.flows} /></div></section>
            <section className="flex min-h-0 flex-col gap-3"><h2 className="text-sm font-semibold text-text-main">Rule prompts</h2><div className="min-h-0 flex-1"><JsonBlock value={workspace.prompts} /></div></section>
          </div>
        ) : null}
        {workspace && activeTab === 'flow' ? <div className="h-full min-h-0 overflow-auto"><FlowViewer yamlContent={workspace.flow_yaml.yaml_content} sourcePath={workspace.flow_yaml.path} /></div> : null}
      </main>
    </div>
  );
}
