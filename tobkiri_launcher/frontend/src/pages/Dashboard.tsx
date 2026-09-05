import type {FormEvent} from 'react';
import {useEffect, useMemo, useState} from 'react';
import {Link, useSearchParams} from 'react-router';
import {
  AlertCircle,
  ArrowRight,
  Monitor,
  Package,
  Plus,
  RefreshCw,
  Search,
  Workflow,
} from 'lucide-react';

import {ProfileCard} from '@/src/components/dashboard/ProfileCard';
import {Button} from '@/src/components/ui/Button';
import {CopyErrorButton} from '@/src/components/ui/CopyErrorButton';
import {Badge} from '@/src/components/ui/Badge';
import {TobkiriLoader, TobkiriLoadingMark} from '@/src/components/ui/TobkiriLoader';
import {
  createNamedProfile,
  deleteNamedProfile,
  duplicateNamedProfile,
  fetchDashboard,
  fetchNamedProfiles,
  isDesktopShellAvailable,
  launchSelectedPresentation,
  updateNamedProfile,
  type NamedProfileRecord,
  type NamedProfileRegistry,
} from '@/src/lib/api';
import {panelRoutes} from '@/src/lib/routes';
import {
  buildNamedProfileView,
  filterAndSortNamedProfiles,
  namedProfileDisplayName,
  type NamedProfileSortMode,
} from '@/src/lib/profileRegistryView';
import {transformDashboard} from '@/src/lib/transforms';
import type {DashboardData} from '@/src/store';
import {useAppStore} from '@/src/store';

const defaultDashboard: DashboardData = {
  kernelStatus: 'stopped',
  uptime: '--',
  activePacks: 0,
  registeredFlows: 0,
  activities: [],
  supervisor: null,
};

export {copyTextToClipboard} from '@/src/lib/clipboard';

export function nextDuplicateProfileId(
  profileId: string,
  existingProfileIds: Iterable<string>,
): string {
  const baseId = `${profileId}-copy`;
  const usedIds = new Set(existingProfileIds);
  let candidate = baseId;
  let suffix = 2;
  while (usedIds.has(candidate)) {
    candidate = `${baseId}-${suffix}`;
    suffix += 1;
  }
  return candidate;
}

function isActiveExecutionProfile(
  registry: NamedProfileRegistry,
  entry: NamedProfileRecord,
): boolean {
  // The active revision is the resolved execution snapshot. It is deliberately
  // not compared with the immutable definition revision on the registry row.
  return registry.active_profile_id === entry.profile_id
    && registry.active_profile_revision !== null;
}

function profileHref(profileId: string, hash?: string): string {
  const query = `?profile_id=${encodeURIComponent(profileId)}`;
  return `${panelRoutes.profile}${query}${hash ? `#${hash}` : ''}`;
}

function sortModeFromParam(value: string | null): NamedProfileSortMode {
  return value === 'recent' || value === 'name' ? value : 'recommended';
}

export function Dashboard() {
  const addToast = useAppStore((state) => state.addToast);
  const showDialog = useAppStore((state) => state.showDialog);
  const runtimeReady = useAppStore((state) => state.runtimeReady);
  const runtimeStatus = useAppStore((state) => state.runtimeStatus);
  const hostCatalogVerified = useAppStore((state) => state.hostCatalogVerified);
  const profileCeremonyAvailable = useAppStore((state) => state.profileCeremonyAvailable);
  const defaultsBootstrapRequired = useAppStore((state) => state.defaultsBootstrapRequired);
  const activeProfileReady = useAppStore((state) => state.activeProfileReady);
  const launchReady = useAppStore((state) => state.launchReady);
  const desktopShellAvailable = isDesktopShellAvailable();
  const [searchParams, setSearchParams] = useSearchParams();

  const [dashboard, setDashboard] = useState<DashboardData>(defaultDashboard);
  const [dashboardLoading, setDashboardLoading] = useState(true);
  const [dashboardError, setDashboardError] = useState<string | null>(null);
  const summaryAvailable = runtimeReady && !dashboardLoading && !dashboardError;
  const [registry, setRegistry] = useState<NamedProfileRegistry | null>(null);
  const [profileLoadError, setProfileLoadError] = useState<string | null>(null);
  const [profileActionError, setProfileActionError] = useState<string | null>(null);
  const [profileBusy, setProfileBusy] = useState<string | null>(null);
  const [newProfileId, setNewProfileId] = useState('');
  const [newProfileName, setNewProfileName] = useState('');
  const [newProfileSourceId, setNewProfileSourceId] = useState('');
  const [showAddProfile, setShowAddProfile] = useState(false);
  const [editingProfileId, setEditingProfileId] = useState<string | null>(null);
  const [editingProfileName, setEditingProfileName] = useState('');

  const profileQuery = searchParams.get('q') ?? '';
  const sortMode = sortModeFromParam(searchParams.get('sort'));
  const browsingProfileId = searchParams.get('profile_id');

  const updateProfileSearch = (key: 'q' | 'sort', value: string) => {
    setSearchParams((current) => {
      const next = new URLSearchParams(current);
      if (value) next.set(key, value);
      else next.delete(key);
      return next;
    }, {replace: true});
  };

  const refreshDashboard = async () => {
    setDashboardLoading(true);
    try {
      const response = await fetchDashboard();
      setDashboard(transformDashboard(response));
      setDashboardError(null);
    } catch (error) {
      const rawMessage = error instanceof Error ? error.message : '';
      setDashboardError(rawMessage || 'Failed to load your workspace summary.');
    } finally {
      setDashboardLoading(false);
    }
  };

  const refreshProfiles = async () => {
    try {
      setRegistry(await fetchNamedProfiles());
      setProfileLoadError(null);
      setProfileActionError(null);
    } catch (error) {
      setProfileLoadError(error instanceof Error ? error.message : 'Named Profiles could not be loaded.');
    }
  };

  useEffect(() => {
    if (runtimeReady) {
      void refreshDashboard();
    } else {
      setDashboardLoading(false);
    }
  }, [runtimeReady]);

  useEffect(() => {
    void refreshProfiles();
  }, []);

  const visibleProfiles = useMemo(() => filterAndSortNamedProfiles(
    registry?.profiles ?? [],
    profileQuery,
    sortMode,
    registry?.active_profile_id ?? null,
  ), [profileQuery, registry, sortMode]);

  const activeProfile = useMemo(() => {
    if (!registry || !registry.active_profile_id || registry.active_profile_revision === null) {
      return null;
    }
    return registry.profiles.find((entry) => isActiveExecutionProfile(registry, entry)) ?? null;
  }, [registry]);
  const browsingProfile = registry?.profiles.find((entry) => entry.profile_id === browsingProfileId) ?? null;
  const profileError = profileLoadError ?? profileActionError;
  const profileCatalogVerified = hostCatalogVerified
    && registry !== null
    && profileLoadError === null;
  const profileActivationAvailable = profileCeremonyAvailable && !defaultsBootstrapRequired;
  const sourceProfileOptions = useMemo(() => (
    [...(registry?.profiles ?? [])].sort((left, right) => {
      const displayNameOrder = namedProfileDisplayName(left).localeCompare(
        namedProfileDisplayName(right),
      );
      return displayNameOrder || left.profile_id.localeCompare(right.profile_id);
    })
  ), [registry]);

  const commitProfileMutation = async (
    key: string,
    operation: () => Promise<NamedProfileRegistry>,
    successMessage: string,
    throwOnError = false,
  ) => {
    if (!profileCatalogVerified) {
      const message = 'Profile catalog verification is required before changing Profiles.';
      setProfileActionError(message);
      addToast(message, 'error');
      return false;
    }
    setProfileBusy(key);
    try {
      setRegistry(await operation());
      setProfileActionError(null);
      addToast(successMessage, 'success');
    } catch (error) {
      const message = error instanceof Error ? error.message : 'Profile mutation was rejected.';
      setProfileActionError(message);
      addToast(message, 'error');
      if (throwOnError) throw error;
      return false;
    } finally {
      setProfileBusy(null);
    }
    return true;
  };

  const submitNewProfile = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const form = event.currentTarget;
    const profileId = (form.elements.namedItem('profile_id') as HTMLInputElement | null)?.value.trim()
      ?? newProfileId.trim();
    const displayName = (form.elements.namedItem('display_name') as HTMLInputElement | null)?.value.trim()
      ?? newProfileName.trim();
    if (!registry) return;
    if (!profileId || !displayName) {
      setProfileActionError('Enter a Profile ID and display name before creating a Profile.');
      return;
    }
    const sourceProfileId = (form.elements.namedItem('source_profile_id') as HTMLSelectElement | null)?.value.trim()
      ?? newProfileSourceId.trim();
    if (!sourceProfileId) {
      setProfileActionError('Choose a source Profile before adding another Profile.');
      return;
    }
    if (!registry.profiles.some((entry) => entry.profile_id === sourceProfileId)) {
      setProfileActionError('Choose an existing Profile as the source for this Profile.');
      return;
    }
    const created = await commitProfileMutation(
      'create',
      () => createNamedProfile({
        profile_id: profileId,
        display_name: displayName,
        source_profile_id: sourceProfileId,
        expected_store_generation: registry.generation,
      }),
      `Profile ${displayName} created.`,
    );
    if (!created) return;
    setNewProfileId('');
    setNewProfileName('');
    setNewProfileSourceId('');
    setShowAddProfile(false);
  };

  const submitProfileName = async (
    event: FormEvent<HTMLFormElement>,
    entry: NamedProfileRecord,
  ) => {
    event.preventDefault();
    if (!registry) return;
    const displayName = (event.currentTarget.elements.namedItem('display_name') as HTMLInputElement | null)?.value.trim()
      ?? editingProfileName.trim();
    if (!displayName) return;
    const updated = await commitProfileMutation(
      `edit:${entry.profile_id}`,
      () => updateNamedProfile({
        profile_id: entry.profile_id,
        display_name: displayName,
        expected_profile_revision: entry.profile_revision,
        expected_store_generation: registry.generation,
      }),
      `Profile ${displayName} updated.`,
    );
    if (!updated) return;
    setEditingProfileId(null);
    setEditingProfileName('');
  };

  const duplicateProfile = async (entry: NamedProfileRecord) => {
    if (!registry) return;
    const candidate = nextDuplicateProfileId(
      entry.profile_id,
      registry.profiles.map((profile) => profile.profile_id),
    );
    const displayName = `${namedProfileDisplayName(entry)} Copy`;
    await commitProfileMutation(
      `duplicate:${entry.profile_id}`,
      () => duplicateNamedProfile({
        profile_id: entry.profile_id,
        new_profile_id: candidate,
        display_name: displayName,
        expected_profile_revision: entry.profile_revision,
        expected_store_generation: registry.generation,
      }),
      `Profile ${displayName} created.`,
    );
  };

  const removeProfile = (entry: NamedProfileRecord) => {
    if (!registry || registry.active_profile_id === entry.profile_id) return;
    const displayName = namedProfileDisplayName(entry);
    showDialog({
      title: `Delete ${displayName}?`,
      message: `This removes ${displayName} from the live Profile registry. Its immutable revision history remains retained by the Host, and the active execution Profile is not changed.`,
      confirmText: 'Delete Profile',
      cancelText: 'Keep Profile',
      onConfirm: async () => {
        await commitProfileMutation(
          `delete:${entry.profile_id}`,
          () => deleteNamedProfile({
            profile_id: entry.profile_id,
            expected_profile_revision: entry.profile_revision,
            expected_store_generation: registry.generation,
          }),
          `Profile ${displayName} deleted.`,
          true,
        );
      },
    });
  };

  const launchProfile = async (entry: NamedProfileRecord) => {
    if (!registry || !isActiveExecutionProfile(registry, entry)) return;
    const profileView = buildNamedProfileView(entry);
    if (
      !activeProfileReady
      || !launchReady
      || !desktopShellAvailable
      || profileView.status !== 'ready'
    ) return;

    setProfileBusy(`launch:${entry.profile_id}`);
    try {
      const result = await launchSelectedPresentation();
      addToast(result.message || `${namedProfileDisplayName(entry)} launched.`, 'success');
    } catch (error) {
      const message = error instanceof Error ? error.message : 'Profile launch was rejected.';
      addToast(message, 'error');
    } finally {
      setProfileBusy(null);
    }
  };

  if (dashboardLoading && !registry && !profileError) {
    return <DashboardSkeleton />;
  }

  return (
    <div className="flex flex-1 overflow-hidden">
      <div className="mx-auto flex w-full max-w-[1100px] flex-col gap-6 overflow-y-auto px-6 py-8 page-enter lg:px-10">
        <section className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
          <div>
            <h1 className="text-2xl font-semibold tracking-tight text-text-main">Home</h1>
            <p className="mt-1 text-sm text-text-muted">Browse every Profile without changing the active execution Profile.</p>
          </div>
          <div className="flex items-center gap-3">
            <Button
              aria-label="Add Profile"
              aria-controls="add-profile-form"
              aria-expanded={showAddProfile}
              disabled={!profileCatalogVerified}
              onClick={() => setShowAddProfile((shown) => !shown)}
              title={profileCatalogVerified ? 'Add a new named Profile' : 'Profile catalog verification is unavailable'}
              type="button"
            >
              <Plus aria-hidden="true" className="h-4 w-4" /> Add Profile
            </Button>
            <Button
              aria-label="Refresh Home and Profiles"
              onClick={() => {
                void refreshDashboard();
                void refreshProfiles();
              }}
              size="icon"
              title="Refresh Home and Profiles"
              type="button"
              variant="outline"
            >
              <RefreshCw aria-hidden="true" className="h-4 w-4" />
            </Button>
          </div>
        </section>

        {dashboardError && (
          <div className="flex items-center gap-3 rounded-lg border border-warning/35 bg-warning/8 px-4 py-3 text-sm text-warning" role="alert">
            <AlertCircle aria-hidden="true" className="h-4 w-4 shrink-0" />
            <span className="flex-1">{dashboardError}</span>
            <CopyErrorButton text={dashboardError} label="Copy dashboard error" />
            <Button onClick={() => void refreshDashboard()} size="sm" type="button" variant="ghost">
              Retry
            </Button>
          </div>
        )}

        {!runtimeReady && runtimeStatus === 'panel_ready' && (
          <div className="flex items-center gap-3 rounded-lg border border-warning/35 bg-warning/8 px-4 py-3 text-sm text-warning" role="status">
            <TobkiriLoadingMark />
            <span className="flex-1">Runtime is still preparing. Profiles and Add remain available; launch and activation wait for readiness.</span>
          </div>
        )}

        <div aria-label="Profile execution status" className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-text-muted">
          <span className="text-xs font-medium text-text-main">Execution</span>
          {activeProfile ? (
            <div className="flex min-w-0 flex-wrap items-center gap-2">
              <span aria-hidden="true" className="h-2 w-2 shrink-0 rounded-full bg-accent" />
              <span className="truncate text-sm font-medium text-text-main">{namedProfileDisplayName(activeProfile)}</span>
              <span className="font-mono text-[11px] text-text-muted">{activeProfile.profile_id}</span>
            </div>
          ) : (
            <Badge variant="warning">No active execution Profile</Badge>
          )}
          {browsingProfileId && (
            <div className="flex min-w-0 flex-wrap items-center gap-2 border-l border-border pl-4">
              <Badge>Selected browsing</Badge>
              <span className="truncate text-sm text-text-main">
                {browsingProfile ? namedProfileDisplayName(browsingProfile) : browsingProfileId}
              </span>
            </div>
          )}
        </div>

        <section aria-labelledby="profiles-title">
          <div className="flex flex-col gap-4 sm:flex-row sm:items-end sm:justify-between">
            <div>
              <h2 className="text-base font-semibold text-text-main" id="profiles-title">Profiles</h2>
              <p className="mt-1 text-xs text-text-muted">
                Browse a Profile without changing execution. Set Active lets you review and approve a Profile before starting it.
              </p>
            </div>
            <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
              <label className="relative block sm:w-64">
                <Search aria-hidden="true" className="pointer-events-none absolute left-3 top-2.5 h-4 w-4 text-text-muted" />
                <input
                  aria-label="Search Profiles"
                  className="h-9 w-full rounded-lg border border-border bg-bg-card pl-9 pr-3 text-sm text-text-main outline-none focus:border-accent focus-visible:ring-2 focus-visible:ring-[var(--ring-color)]"
                  onChange={(event) => updateProfileSearch('q', event.target.value)}
                  placeholder="Search Profiles"
                  type="search"
                  value={profileQuery}
                />
              </label>
              <label className="sr-only" htmlFor="profile-sort">Sort Profiles</label>
              <select
                aria-label="Sort Profiles"
                className="h-9 rounded-lg border border-border bg-bg-card px-3 text-sm text-text-main outline-none focus:border-accent focus-visible:ring-2 focus-visible:ring-[var(--ring-color)]"
                id="profile-sort"
                onChange={(event) => updateProfileSearch('sort', event.target.value === 'recommended' ? '' : event.target.value)}
                value={sortMode}
              >
                <option value="recommended">Recommended</option>
                <option value="recent">Recently updated</option>
                <option value="name">Name</option>
              </select>
            </div>
          </div>

          {showAddProfile && (
            <form
              aria-describedby="add-profile-help"
              className="mt-4 grid gap-3 rounded-lg border border-border bg-bg-card p-4 sm:grid-cols-[1fr_1fr_1fr_auto] sm:items-end"
              id="add-profile-form"
              onSubmit={submitNewProfile}
            >
              <label className="space-y-1.5 text-sm font-medium text-text-main">
                <span>Profile ID <span aria-hidden="true" className="text-destructive">*</span></span>
                <input
                  aria-label="New Profile ID"
                  className="h-9 w-full rounded-lg border border-border bg-bg-main px-3 text-sm font-normal text-text-main outline-none focus:border-accent focus-visible:ring-2 focus-visible:ring-[var(--ring-color)]"
                  maxLength={80}
                  name="profile_id"
                  onChange={(event) => setNewProfileId(event.target.value)}
                  pattern="[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*"
                  placeholder="profile-id"
                  required
                  value={newProfileId}
                />
              </label>
              <label className="space-y-1.5 text-sm font-medium text-text-main">
                <span>Display name <span aria-hidden="true" className="text-destructive">*</span></span>
                <input
                  aria-label="New Profile name"
                  className="h-9 w-full rounded-lg border border-border bg-bg-main px-3 text-sm font-normal text-text-main outline-none focus:border-accent focus-visible:ring-2 focus-visible:ring-[var(--ring-color)]"
                  maxLength={120}
                  name="display_name"
                  onChange={(event) => setNewProfileName(event.target.value)}
                  placeholder="Display name"
                  required
                  value={newProfileName}
                />
              </label>
              <label className="space-y-1.5 text-sm font-medium text-text-main">
                <span>Source Profile <span aria-hidden="true" className="text-destructive">*</span></span>
                <select
                  aria-label="Source Profile"
                  className="h-9 w-full rounded-lg border border-border bg-bg-main px-3 text-sm font-normal text-text-main outline-none focus:border-accent focus-visible:ring-2 focus-visible:ring-[var(--ring-color)]"
                  onChange={(event) => setNewProfileSourceId(event.target.value)}
                  name="source_profile_id"
                  required
                  value={newProfileSourceId}
                >
                  <option disabled value="">Choose a source Profile</option>
                  {sourceProfileOptions.map((entry) => (
                    <option key={entry.profile_id} value={entry.profile_id}>
                      {namedProfileDisplayName(entry)} ({entry.profile_id})
                    </option>
                  ))}
                </select>
              </label>
              <div className="flex flex-col gap-2">
                <span className="sr-only" id="add-profile-help">Create a named Profile by explicitly choosing an existing source Profile.</span>
                <Button
                  disabled={!profileCatalogVerified || !newProfileSourceId || profileBusy === 'create'}
                  size="sm"
                  type="submit"
                >
                  <Plus aria-hidden="true" className="h-3.5 w-3.5" /> Create
                </Button>
              </div>
            </form>
          )}

          {profileError && (
            <div aria-live="assertive" className="mt-4 flex items-center gap-2 rounded-lg border border-destructive/35 bg-destructive/8 px-3 py-2 text-sm text-destructive" role="alert">
              <AlertCircle aria-hidden="true" className="h-4 w-4 shrink-0" />
              <span className="flex-1">{profileError}</span>
              <CopyErrorButton text={profileError} label="Copy Profile error" />
              <Button
                onClick={() => {
                  if (profileLoadError) {
                    void refreshProfiles();
                  } else {
                    setProfileActionError(null);
                  }
                }}
                size="sm"
                type="button"
                variant="ghost"
              >
                {profileLoadError ? 'Retry' : 'Dismiss'}
              </Button>
            </div>
          )}

          <div aria-live="polite" className="mt-5">
            {!registry && !profileError && (
              <div className="flex items-center justify-center py-8"><TobkiriLoadingMark /></div>
            )}
            {registry && visibleProfiles.length === 0 && profileQuery && (
              <p className="py-8 text-center text-sm text-text-muted">No Profiles match this search.</p>
            )}
            {registry && visibleProfiles.length === 0 && !profileQuery && (
              <div className="flex flex-col items-center justify-center rounded-xl border border-dashed border-border bg-bg-card px-6 py-10 text-center">
                <h3 className="text-sm font-semibold text-text-main">No Profiles yet</h3>
                <p className="mt-1 max-w-sm text-xs text-text-muted">
                  Add a named Profile to begin browsing your own Profile catalog.
                </p>
                <Button
                  aria-label="Create Profile"
                  className="mt-4"
                  disabled={!profileCatalogVerified}
                  onClick={() => setShowAddProfile(true)}
                  type="button"
                >
                  <Plus aria-hidden="true" className="h-4 w-4" /> Add Profile
                </Button>
              </div>
            )}
            {registry && visibleProfiles.length > 0 && (
              <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3" data-testid="profile-grid">
                {visibleProfiles.map((entry) => {
                  const active = isActiveExecutionProfile(registry, entry);
                  const profileView = buildNamedProfileView(entry);
                  // Only the card whose own action is in flight reports busy.
                  // Any pending mutation still locks the catalog for every card
                  // through mutationsAvailable, so a create cannot race a rename.
                  const cardBusyKey = profileBusy?.endsWith(`:${entry.profile_id}`) === true
                    ? profileBusy
                    : null;
                  const busy = cardBusyKey !== null;
                  return (
                    <ProfileCard
                      activationHref={profileHref(entry.profile_id, 'profile-ceremony')}
                      actionType={cardBusyKey?.split(':')[0] ?? null}
                      browseHref={profileHref(entry.profile_id)}
                      closureHref={profileHref(entry.profile_id, 'profile-closure')}
                      desktopShellAvailable={desktopShellAvailable}
                      editing={editingProfileId === entry.profile_id}
                      editingName={editingProfileName}
                      isActive={active}
                      isBrowsing={browsingProfileId === entry.profile_id}
                      isBusy={busy}
                      key={entry.profile_id}
                      mutationsAvailable={profileCatalogVerified && profileBusy === null}
                      onCancelEdit={() => {
                        setEditingProfileId(null);
                        setEditingProfileName('');
                      }}
                      onDelete={removeProfile}
                      onDuplicate={(profile) => void duplicateProfile(profile)}
                      onEdit={(profile) => {
                        setEditingProfileId(profile.profile_id);
                        setEditingProfileName(namedProfileDisplayName(profile));
                      }}
                      onEditingNameChange={setEditingProfileName}
                      onLaunch={(profile) => void launchProfile(profile)}
                      onSubmitEdit={(event, profile) => void submitProfileName(event, profile)}
                      profile={entry}
                      profileView={profileView}
                      profileCeremonyAvailable={profileActivationAvailable}
                      activeProfileReady={activeProfileReady}
                      launchReady={launchReady}
                    />
                  );
                })}
              </div>
            )}
          </div>
        </section>

        <section aria-label="Workspace summary" className="grid gap-4 sm:grid-cols-3">
          <Link
            className="group rounded-xl border border-border bg-bg-card p-4 transition-colors hover:bg-bg-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--ring-color)]"
            to={panelRoutes.packs}
          >
            <div className="flex items-center gap-2">
              <Package aria-hidden="true" className="h-4 w-4 shrink-0 text-text-muted" />
              <h3 className="text-sm font-semibold text-text-main group-hover:underline">Active Packs</h3>
              <ArrowRight aria-hidden="true" className="ml-auto h-4 w-4 shrink-0 text-text-muted" />
            </div>
            <div className="mt-2 text-2xl font-semibold tracking-tight text-text-main">{summaryAvailable ? dashboard.activePacks : '--'}</div>
            <p className="mt-1 text-xs text-text-muted">Enabled in the current v4 Profile</p>
          </Link>
          <div className="rounded-xl border border-border bg-bg-card p-4">
            <div className="flex items-center gap-2">
              <Workflow aria-hidden="true" className="h-4 w-4 shrink-0 text-text-muted" />
              <h3 className="text-sm font-semibold text-text-main">Flows</h3>
            </div>
            <div className="mt-2 text-2xl font-semibold tracking-tight text-text-main">{summaryAvailable ? dashboard.registeredFlows : '--'}</div>
            <p className="mt-1 text-xs text-text-muted">Registered flow definitions</p>
          </div>
          <div className="rounded-xl border border-border bg-bg-card p-4">
            <div className="flex items-center gap-2">
              <Monitor aria-hidden="true" className="h-4 w-4 shrink-0 text-text-muted" />
              <h3 className="text-sm font-semibold text-text-main">Kernel</h3>
            </div>
            <div className="mt-2 flex items-center gap-2">
              <span
                aria-hidden="true"
                className={summaryAvailable && dashboard.kernelStatus === 'running' ? 'h-2.5 w-2.5 shrink-0 rounded-full bg-success' : 'h-2.5 w-2.5 shrink-0 rounded-full bg-warning'}
              />
              <span className="text-lg font-semibold tracking-tight text-text-main">
                {!summaryAvailable ? 'Not verified' : dashboard.kernelStatus === 'running' ? 'Running' : dashboard.kernelStatus === 'error' ? 'Error' : 'Stopped'}
              </span>
            </div>
            <p className="mt-1 text-xs text-text-muted">Uptime: {summaryAvailable ? dashboard.uptime : '--'}</p>
          </div>
        </section>
      </div>
    </div>
  );
}

function DashboardSkeleton() {
  return <TobkiriLoader label="Loading Tobkiri home..." />;
}
