import { useEffect, useMemo, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router';
import {
  activateStartupProfile,
  addPackToStartupProfile,
  approvePack,
  clearStartupProfileNodeOverride,
  compileStartupProfilePreview,
  createStartupProfile,
  deleteStartupProfile,
  duplicateStartupProfile,
  fetchDashboard,
  fetchStartupProfiles,
  launchDefaultspackDesktop,
  launchStartupProfile,
  removePackFromStartupProfile,
  setStartupProfileNodeOverride,
  updateStartupProfile,
} from '@/src/lib/api';
import type {
  ApiStartupProfile,
  StartupProfileMutationResponseData,
  StartupProfileCompilePreviewResponseData,
  StartupProfilesResponseData,
} from '@/src/lib/apiTypes';
import {
  buildStartupProfileView,
  compatibleNodesForPort,
  describeStartupActionError,
  describeStartupIssue,
  filterAndSortStartupProfiles,
  packLabel,
  titleCasePortKey,
  type StartupSortMode,
} from '@/src/lib/startupProfiles';
import { apiMapRoute, profileGraphRoute } from '@/src/lib/routes';
import { useAppStore } from '@/src/store';
import { TobkiriLoader, TobkiriLoadingMark } from '@/src/components/ui/TobkiriLoader';
import {
  AlertCircle,
  ArrowLeft,
  CheckCircle2,
  Cloud,
  Copy,
  Monitor,
  MoreHorizontal,
  Plus,
  RefreshCw,
  Rocket,
  Route,
  Save,
  Search,
  Share2,
  ShieldCheck,
  Star,
  Terminal,
  Trash2,
} from 'lucide-react';
import { Popover, PopoverTrigger, PopoverContent } from '@/src/components/ui/Popover';
import { Button } from '@/src/components/ui/Button';
import { Badge } from '@/src/components/ui/Badge';
import { transformDashboard } from '@/src/lib/transforms';
import type { DashboardData } from '@/src/store';

type ActionState = 'activate' | 'approve' | 'create' | 'delete' | 'duplicate' | 'launch' | 'save';

const defaultDashboard: DashboardData = {
  kernelStatus: 'stopped',
  uptime: '--',
  activePacks: 0,
  registeredFlows: 0,
  activities: [],
  supervisor: null,
};
const INITIAL_PROFILE_LOAD_MAX_ATTEMPTS = 3;
const INITIAL_PROFILE_LOAD_RETRY_DELAY_MS = 900;

export function canLoadDashboardProfiles(runtimeReady: boolean, runtimeStatus: string): boolean {
  // Reaching this route means the panel HTTP server is already available.
  // Start the profile request immediately so a delayed health poll cannot leave
  // the home screen behind an indefinite startup loader.
  return runtimeReady || runtimeStatus !== 'error';
}

export function buildProfileCreationRequest(
  name: string,
  basePack: string,
): { name: string; base_pack: string } | null {
  const normalizedBasePack = basePack.trim();
  if (!normalizedBasePack) return null;
  return {
    name: name.trim() || 'New Profile',
    base_pack: normalizedBasePack,
  };
}

const PROFILE_DRAFT_STORAGE_PREFIX = 'tobkiri-profile-draft:';

export function readProfileDraft(profile: ApiStartupProfile): ApiStartupProfile | null {
  if (typeof window === 'undefined') return null;
  try {
    const stored = window.sessionStorage.getItem(`${PROFILE_DRAFT_STORAGE_PREFIX}${profile.profile_id}`);
    if (!stored) return null;
    const parsed = JSON.parse(stored) as ApiStartupProfile;
    return parsed.profile_id === profile.profile_id ? parsed : null;
  } catch {
    return null;
  }
}

export function writeProfileDraft(profile: ApiStartupProfile): void {
  if (typeof window === 'undefined') return;
  window.sessionStorage.setItem(
    `${PROFILE_DRAFT_STORAGE_PREFIX}${profile.profile_id}`,
    JSON.stringify(profile),
  );
}

export function clearProfileDraft(profileId: string): void {
  if (typeof window === 'undefined') return;
  window.sessionStorage.removeItem(`${PROFILE_DRAFT_STORAGE_PREFIX}${profileId}`);
}

export async function launchStartupProfileFromDashboard({
  profileId,
  preferredProfileId,
  launchProfile,
  refreshProfiles,
  refreshDashboard,
  openDesktop,
  setSuccessFeedback,
  setErrorFeedback,
  translateError,
}: {
  profileId: string;
  preferredProfileId?: string | null;
  launchProfile: (profileId: string) => Promise<StartupProfileMutationResponseData>;
  refreshProfiles: (preferredProfileId?: string | null) => Promise<void>;
  refreshDashboard: () => Promise<void>;
  openDesktop: () => Promise<unknown>;
  setSuccessFeedback: (message: string) => void;
  setErrorFeedback: (message: string) => void;
  translateError: (error: unknown, fallbackAction: string) => string;
}): Promise<void> {
  await launchProfile(profileId);

  await refreshProfiles(preferredProfileId);
  await refreshDashboard();
  try {
    await openDesktop();
  } catch (desktopError) {
    setErrorFeedback(`Profile launched, but Defaultspack desktop did not open: ${translateError(desktopError, 'open Defaultspack desktop')}`);
    return;
  }
  setSuccessFeedback('Profile launched. Defaultspack window opened.');
}

function formatTimestamp(timestamp: number): string {
  if (!timestamp) return '--';
  return new Date(timestamp * 1000).toLocaleString();
}

function shouldRetryInitialProfileLoad(errorMessage: string): boolean {
  return /Unauthorized|Invalid or expired code|Too many requests|429|Failed to fetch|NetworkError/i.test(errorMessage);
}

export async function copyTextToClipboard(
  text: string,
  clipboard: Pick<Clipboard, 'writeText'> | undefined = typeof navigator === 'undefined'
    ? undefined
    : navigator.clipboard,
): Promise<boolean> {
  if (!clipboard) return false;

  try {
    await clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}

export function Dashboard() {
  const addToast = useAppStore((state) => state.addToast);
  const showDialog = useAppStore((state) => state.showDialog);
  const runtimeReady = useAppStore((state) => state.runtimeReady);
  const runtimeStatus = useAppStore((state) => state.runtimeStatus);
  const runtimeError = useAppStore((state) => state.runtimeError);
  const profilesAvailable = canLoadDashboardProfiles(runtimeReady, runtimeStatus);
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();

  const [dashboard, setDashboard] = useState<DashboardData>(defaultDashboard);
  const [dashboardLoading, setDashboardLoading] = useState(true);
  const [dashboardError, setDashboardError] = useState<string | null>(null);

  const [payload, setPayload] = useState<StartupProfilesResponseData | null>(null);
  const [profilesLoading, setProfilesLoading] = useState(true);
  const [profilesError, setProfilesError] = useState<string | null>(null);
  const [feedback, setFeedback] = useState<{ message: string; tone: 'error' | 'success' } | null>(null);
  const [actionState, setActionState] = useState<{ profileId?: string; type: ActionState } | null>(null);
  const [draft, setDraft] = useState<ApiStartupProfile | null>(null);
  const [createDialogOpen, setCreateDialogOpen] = useState(false);
  const [newProfileName, setNewProfileName] = useState('');
  const [newProfileBasePack, setNewProfileBasePack] = useState('');

  const editProfileId = searchParams.get('edit');
  const searchQuery = searchParams.get('q') ?? '';
  const sortMode = ((): StartupSortMode => {
    const value = searchParams.get('sort');
    return value === 'name' || value === 'recent' || value === 'recommended' ? value : 'recommended';
  })();

  const patchSearchParams = (updates: Record<string, string | null>) => {
    const next = new URLSearchParams(searchParams);
    Object.entries(updates).forEach(([key, value]) => {
      if (value) {
        next.set(key, value);
      } else {
        next.delete(key);
      }
    });
    setSearchParams(next, { replace: true });
  };

  const translateActionError = (error: unknown, fallbackAction: string) => {
    const rawMessage = error instanceof Error ? error.message : '';
    return describeStartupActionError(rawMessage, fallbackAction);
  };

  const copyRuntimeError = async () => {
    const message = runtimeError || 'The control panel opened, but the background runtime startup failed.';
    const copied = await copyTextToClipboard(message);
    addToast(
      copied ? 'Error copied to clipboard.' : 'Could not copy the error. Please select and copy it manually.',
      copied ? 'success' : 'error',
    );
  };

  const refreshDashboard = async () => {
    setDashboardLoading(true);
    try {
      const response = await fetchDashboard();
      setDashboard(transformDashboard(response));
      setDashboardError(null);
    } catch (error) {
      setDashboardError(translateActionError(error, 'load your workspace summary'));
    } finally {
      setDashboardLoading(false);
    }
  };

  const refreshProfiles = async (preferredProfileId?: string | null) => {
    setProfilesLoading(true);
    const maxAttempts = payload ? 1 : INITIAL_PROFILE_LOAD_MAX_ATTEMPTS;
    let lastErrorMessage = '';

    try {
      for (let attempt = 1; attempt <= maxAttempts; attempt += 1) {
        try {
          const response = await fetchStartupProfiles();
          setPayload(response);
          setProfilesError(null);

          if (preferredProfileId) {
            patchSearchParams({ edit: preferredProfileId });
          } else if (editProfileId && !response.profiles.some((profile) => profile.profile_id === editProfileId)) {
            patchSearchParams({ edit: null });
          }
          return;
        } catch (error) {
          lastErrorMessage = translateActionError(error, 'load startup profiles');
          const shouldRetry = attempt < maxAttempts && shouldRetryInitialProfileLoad(lastErrorMessage);
          if (!shouldRetry) break;
          await new Promise<void>((resolve) => {
            window.setTimeout(resolve, INITIAL_PROFILE_LOAD_RETRY_DELAY_MS * attempt);
          });
        }
      }
      setProfilesError(lastErrorMessage || 'The launcher could not load your profiles yet.');
    } finally {
      setProfilesLoading(false);
    }
  };

  useEffect(() => {
    if (!profilesAvailable) return;
    if (runtimeReady) {
      void refreshDashboard();
    } else {
      setDashboardLoading(false);
    }
    void refreshProfiles();
  }, [profilesAvailable, runtimeReady]);

  const selectedProfile = useMemo(
    () => payload?.profiles.find((profile) => profile.profile_id === editProfileId) ?? null,
    [editProfileId, payload],
  );

  useEffect(() => {
    if (!selectedProfile) { setDraft(null); return; }
    setDraft(readProfileDraft(selectedProfile) ?? JSON.parse(JSON.stringify(selectedProfile)));
  }, [selectedProfile]);

  useEffect(() => {
    if (draft) writeProfileDraft(draft);
  }, [draft]);

  const profileViews = useMemo(() => {
    if (!payload) return [];
    return payload.profiles.map((profile) =>
      buildStartupProfileView(profile, payload.catalog, payload.active_profile_id, payload.last_launched_profile_id),
    );
  }, [payload]);

  const visibleProfiles = useMemo(
    () => filterAndSortStartupProfiles(profileViews, searchQuery, sortMode),
    [profileViews, searchQuery, sortMode],
  );

  const catalog = payload?.catalog ?? null;
  const profileCount = payload?.profiles.length ?? 0;
  const catalogPacks = catalog?.packs ?? [];
  const selectedBasePack = catalogPacks.find((pack) => pack.pack_id === draft?.base_pack) ?? null;
  const availablePacksToAdd = useMemo(
    () => catalogPacks.filter((pack) => pack.available && draft && !draft.packs.includes(pack.pack_id)),
    [catalogPacks, draft],
  );

  const isDirty = useMemo(() => {
    if (!selectedProfile || !draft) return false;
    return JSON.stringify({
      name: selectedProfile.name, base_pack: selectedProfile.base_pack,
      graph_id: selectedProfile.graph_id, packs: selectedProfile.packs,
      node_overrides: selectedProfile.node_overrides, icon: selectedProfile.icon ?? null,
    }) !== JSON.stringify({
      name: draft.name, base_pack: draft.base_pack,
      graph_id: draft.graph_id, packs: draft.packs,
      node_overrides: draft.node_overrides, icon: draft.icon ?? null,
    });
  }, [draft, selectedProfile]);

  const setSuccessFeedback = (message: string) => {
    setFeedback({ message, tone: 'success' });
    addToast(message, 'success');
  };

  const setErrorFeedback = (message: string) => {
    setFeedback({ message, tone: 'error' });
    addToast(message, 'error');
  };

  const openCreateDialog = () => {
    setNewProfileName('');
    setNewProfileBasePack('');
    setCreateDialogOpen(true);
  };

  const handleCreate = async () => {
    setActionState({ type: 'create' });
    setFeedback(null);
    try {
      const request = buildProfileCreationRequest(newProfileName, newProfileBasePack);
      if (!request) throw new Error('Choose a base pack before creating this profile.');
      const response = await createStartupProfile(request);
      await refreshProfiles(response.profile.profile_id);
      setCreateDialogOpen(false);
      setSuccessFeedback('Custom profile created.');
    } catch (error) {
      setErrorFeedback(translateActionError(error, 'create a custom profile'));
    } finally { setActionState(null); }
  };

  const handleSave = async () => {
    if (!draft) return;
    setActionState({ type: 'save', profileId: draft.profile_id });
    setFeedback(null);
    try {
      await updateStartupProfile(draft.profile_id, {
        name: draft.name, base_pack: draft.base_pack, graph_id: draft.graph_id,
        packs: draft.packs, node_overrides: draft.node_overrides, icon: draft.icon ?? null,
      });
      clearProfileDraft(draft.profile_id);
      await refreshProfiles(draft.profile_id);
      setSuccessFeedback('Profile changes saved.');
    } catch (error) {
      setErrorFeedback(translateActionError(error, 'save this profile'));
    } finally { setActionState(null); }
  };

  const handleAddPack = async (packId: string) => {
    if (!draft || !packId) return;
    setActionState({ type: 'save', profileId: draft.profile_id });
    setFeedback(null);
    try {
      const response = await addPackToStartupProfile(draft.profile_id, packId);
      setDraft(response.profile);
      await refreshProfiles(draft.profile_id);
      setSuccessFeedback('Pack added to profile.');
    } catch (error) {
      setErrorFeedback(translateActionError(error, 'add this pack'));
    } finally { setActionState(null); }
  };

  const handleApprovePack = async (packId: string) => {
    setActionState({ type: 'approve', profileId: editProfileId ?? undefined });
    setFeedback(null);
    try {
      await approvePack(packId);
      await refreshProfiles(editProfileId);
      setSuccessFeedback('Pack approved. You can now use it in this profile.');
    } catch (error) {
      setErrorFeedback(translateActionError(error, 'approve this pack'));
    } finally {
      setActionState(null);
    }
  };

  const handleRemovePack = async (packId: string) => {
    if (!draft || packId === draft.base_pack) return;
    setActionState({ type: 'save', profileId: draft.profile_id });
    setFeedback(null);
    try {
      const response = await removePackFromStartupProfile(draft.profile_id, packId);
      setDraft(response.profile);
      await refreshProfiles(draft.profile_id);
      setSuccessFeedback('Pack removed from profile.');
    } catch (error) {
      setErrorFeedback(translateActionError(error, 'remove this pack'));
    } finally { setActionState(null); }
  };

  const handleOverrideChange = async (portKey: string, nodeId: string) => {
    if (!draft) return;
    setActionState({ type: 'save', profileId: draft.profile_id });
    setFeedback(null);
    try {
      const response = nodeId
        ? await setStartupProfileNodeOverride(draft.profile_id, portKey, nodeId)
        : await clearStartupProfileNodeOverride(draft.profile_id, portKey);
      setDraft(response.profile);
      await refreshProfiles(draft.profile_id);
      setSuccessFeedback(nodeId ? 'Node override saved.' : 'Node override cleared.');
    } catch (error) {
      setErrorFeedback(translateActionError(error, 'update this override'));
    } finally { setActionState(null); }
  };

  const handleDuplicate = async (profileId: string) => {
    setActionState({ type: 'duplicate', profileId });
    setFeedback(null);
    try {
      const response = await duplicateStartupProfile(profileId);
      await refreshProfiles(response.profile.profile_id);
      setSuccessFeedback('Profile duplicated.');
    } catch (error) {
      setErrorFeedback(translateActionError(error, 'duplicate this profile'));
    } finally { setActionState(null); }
  };

  const handleActivate = async (profileId: string) => {
    setActionState({ type: 'activate', profileId });
    setFeedback(null);
    try {
      await activateStartupProfile(profileId);
      await refreshProfiles(profileId);
      setSuccessFeedback('Active profile updated for the next launch.');
    } catch (error) {
      setErrorFeedback(translateActionError(error, 'set this profile as active'));
    } finally { setActionState(null); }
  };

  const handleLaunch = async (profileId: string) => {
    setActionState({ type: 'launch', profileId });
    setFeedback(null);
    try {
      await launchStartupProfileFromDashboard({
        profileId,
        preferredProfileId: editProfileId,
        launchProfile: launchStartupProfile,
        refreshProfiles,
        refreshDashboard,
        openDesktop: launchDefaultspackDesktop,
        setSuccessFeedback,
        setErrorFeedback,
        translateError: translateActionError,
      });
    } catch (error) {
      setErrorFeedback(translateActionError(error, 'launch this profile'));
    } finally { setActionState(null); }
  };

  const handleOpenProfileGraph = (profileId: string) => {
    if (draft?.profile_id === profileId) writeProfileDraft(draft);
    navigate(profileGraphRoute(profileId));
  };

  const handleOpenApiMap = (profileId: string) => {
    if (draft?.profile_id === profileId) writeProfileDraft(draft);
    navigate(apiMapRoute({
      profileId,
      focus: `profile:${profileId}`,
    }));
  };

  const handleDelete = (profileId: string, name: string) => {
    showDialog({
      title: 'Delete this profile?',
      message: profileCount <= 1
        ? 'At least one startup profile must remain.'
        : `Delete '${name}' and switch back to another saved profile?`,
      confirmText: 'Delete',
      onConfirm: async () => {
        if (profileCount <= 1) return;
        setActionState({ type: 'delete', profileId });
        setFeedback(null);
        try {
          const response = await deleteStartupProfile(profileId);
          if (editProfileId === profileId) patchSearchParams({ edit: null });
          await refreshProfiles(response.active_profile_id);
          setSuccessFeedback('Profile deleted.');
        } catch (error) {
          setErrorFeedback(translateActionError(error, 'delete this profile'));
          throw error;
        } finally { setActionState(null); }
      },
    });
  };

  // --- Loading / Error states ---

  if (!profilesAvailable && runtimeStatus !== 'error') {
    return <DashboardSkeleton />;
  }

  if (runtimeStatus === 'error' && !payload) {
    return (
      <div className="flex flex-1 items-center justify-center px-6">
        <div className="flex max-w-md flex-col gap-4 rounded-xl border border-red-200 bg-red-50 p-6 dark:border-red-900/40 dark:bg-red-950/20">
          <div className="flex items-start gap-3">
            <AlertCircle className="mt-0.5 h-5 w-5 text-red-500 shrink-0" />
            <div className="space-y-1">
              <h2 className="text-base font-semibold text-text-main">Runtime could not finish starting</h2>
              <div className="flex items-start gap-1">
                <p className="text-sm text-text-muted">{runtimeError || 'The control panel opened, but the background runtime startup failed.'}</p>
                <Button
                  aria-label="Copy error message"
                  className="h-7 w-7 shrink-0 p-0"
                  onClick={() => void copyRuntimeError()}
                  size="icon"
                  title="Copy error message"
                  variant="ghost"
                >
                  <Copy className="h-3.5 w-3.5" />
                </Button>
              </div>
            </div>
          </div>
          <div className="flex justify-end">
            <Button onClick={() => window.location.reload()} size="sm"><RefreshCw className="h-3.5 w-3.5" /> Reload</Button>
          </div>
        </div>
      </div>
    );
  }

  if (profilesLoading && !payload) return <DashboardSkeleton />;

  if (!payload) {
    return (
      <div className="flex flex-1 items-center justify-center px-6">
        <div className="flex max-w-md flex-col gap-4 rounded-xl border border-border bg-bg-card p-6">
          <div className="flex items-start gap-3">
            <AlertCircle className="mt-0.5 h-5 w-5 text-red-500 shrink-0" />
            <div className="space-y-1">
              <h2 className="text-base font-semibold text-text-main">Home could not load</h2>
              <p className="text-sm text-text-muted">{profilesError || 'The launcher could not load your profiles yet.'}</p>
            </div>
          </div>
          <div className="flex justify-end">
            <Button onClick={() => void refreshProfiles()} size="sm"><RefreshCw className="h-3.5 w-3.5" /> Retry</Button>
          </div>
        </div>
      </div>
    );
  }

  // --- Main render ---

  return (
    <div className="flex flex-1 overflow-hidden">
      <div className="mx-auto flex w-full max-w-[1100px] flex-col gap-6 px-6 py-8 lg:px-10 scrollbar-hidden overflow-y-auto page-enter">
        {/* Header section */}
        <section className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
          <div>
            <h1 className="text-2xl font-semibold tracking-tight text-text-main">My Profiles</h1>
            <p className="mt-1 text-sm text-text-muted">Launch and manage your startup profiles.</p>
          </div>
          <div className="flex items-center gap-3">
            <label className="flex items-center gap-2 rounded-lg border border-border bg-bg-card px-3 py-2 text-sm">
              <Search className="h-4 w-4 text-text-muted" />
              <input
                value={searchQuery}
                onChange={(e) => patchSearchParams({ q: e.target.value || null })}
                placeholder="Search profiles..."
                className="w-36 bg-transparent text-sm text-text-main outline-none placeholder:text-text-muted sm:w-48"
                aria-label="Search profiles"
              />
            </label>
            <Button onClick={openCreateDialog}>
              <Plus className="h-4 w-4" /> Create
            </Button>
          </div>
        </section>

        {/* Feedback banner */}
        {feedback && (
          <div className={`flex items-center gap-3 rounded-lg border px-4 py-3 text-sm ${
            feedback.tone === 'error'
              ? 'border-red-200 bg-red-50 text-red-700 dark:border-red-900/40 dark:bg-red-950/30 dark:text-red-300'
              : 'border-emerald-200 bg-emerald-50 text-emerald-700 dark:border-emerald-900/40 dark:bg-emerald-950/20 dark:text-emerald-300'
          }`}>
            {feedback.tone === 'error' ? <AlertCircle className="h-4 w-4 shrink-0" /> : <CheckCircle2 className="h-4 w-4 shrink-0" />}
            <span className="flex-1">{feedback.message}</span>
            {feedback.tone === 'error' && (
              <Button variant="ghost" size="sm" onClick={() => void refreshProfiles(editProfileId)}>
                <RefreshCw className="h-3.5 w-3.5" /> Retry
              </Button>
            )}
          </div>
        )}

        {!runtimeReady && runtimeStatus === 'panel_ready' && (
          <div className="flex items-center gap-3 rounded-lg border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-800 dark:border-amber-900/40 dark:bg-amber-950/20 dark:text-amber-200">
            <TobkiriLoadingMark />
            <span className="flex-1">Runtime is still preparing. Profiles are available now, and launch surfaces will open after readiness.</span>
          </div>
        )}

        {/* Profile Grid */}
        {visibleProfiles.length === 0 ? (
          <section className="rounded-xl border border-dashed border-border bg-bg-card/50 px-8 py-16 text-center">
            <div className="mx-auto flex max-w-sm flex-col items-center gap-4">
              <div className="flex h-12 w-12 items-center justify-center rounded-full bg-bg-hover">
                {searchQuery ? <Search className="h-5 w-5 text-text-muted" /> : <Plus className="h-5 w-5 text-text-muted" />}
              </div>
              <div className="space-y-1">
                <h2 className="text-base font-semibold text-text-main">
                  {searchQuery ? 'No profiles match that search' : 'Create your first profile'}
                </h2>
                <p className="text-sm text-text-muted">
                  {searchQuery ? 'Try a different search term.' : 'Profiles keep your preferred settings ready for launch.'}
                </p>
              </div>
              {searchQuery ? (
                <Button variant="outline" size="sm" onClick={() => patchSearchParams({ q: null })}>Clear search</Button>
              ) : (
                <Button size="sm" onClick={openCreateDialog}>Create Profile</Button>
              )}
            </div>
          </section>
        ) : (
          <section className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
            {visibleProfiles.map((profileView) => {
              const { issues, profile, runtimeReady: profileReady, basePack, lastLaunched } = profileView;
              const hasDanger = issues.some((i) => i.severity === 'danger');
              const isActive = payload.active_profile_id === profile.profile_id;
              const busy = actionState?.profileId === profile.profile_id;

              return (
                <article
                  key={profile.profile_id}
                  className={`group relative flex flex-col rounded-xl border bg-bg-card p-5 transition-all hover:shadow-[var(--shadow-md)] ${
                    isActive ? 'border-accent/25' : 'border-border'
                  }`}
                >
                  {/* Top: status + menu */}
                  <div className="flex items-center justify-between">
                    <div className="flex items-center gap-1.5">
                      {isActive && (
                        <>
                          <span className="h-2 w-2 rounded-full bg-accent" />
                          <span className="text-[11px] font-medium text-accent">Active</span>
                        </>
                      )}
                      {!isActive && lastLaunched && <span className="text-[11px] text-text-muted">Last used</span>}
                    </div>
                    <Popover>
                      <PopoverTrigger className="rounded-md p-1.5 text-text-muted transition hover:bg-bg-hover">
                        <MoreHorizontal className="h-4 w-4" />
                        <span className="sr-only">Actions</span>
                      </PopoverTrigger>
                      <PopoverContent className="w-40">
                        <div className="flex flex-col py-1">
                          <button role="menuitem" onClick={() => patchSearchParams({ edit: profile.profile_id })} className="flex items-center gap-2 rounded-md px-3 py-2 text-left text-sm text-text-main transition hover:bg-bg-hover">
                            <Save className="h-3.5 w-3.5" /> Edit
                          </button>
                          <button role="menuitem" onClick={() => void handleActivate(profile.profile_id)} disabled={isActive || busy} className="flex items-center gap-2 rounded-md px-3 py-2 text-left text-sm text-text-main transition hover:bg-bg-hover disabled:opacity-50">
                            <Star className={`h-3.5 w-3.5 ${isActive ? 'fill-accent text-accent' : ''}`} /> {isActive ? 'Active' : 'Set Active'}
                          </button>
                          <button role="menuitem" onClick={() => void handleDuplicate(profile.profile_id)} disabled={busy} className="flex items-center gap-2 rounded-md px-3 py-2 text-left text-sm text-text-main transition hover:bg-bg-hover disabled:opacity-50">
                            <Copy className="h-3.5 w-3.5" /> Duplicate
                          </button>
                          <div className="my-1 border-t border-border" />
                          <button role="menuitem" onClick={() => handleDelete(profile.profile_id, profile.name)} disabled={profileCount <= 1 || busy} className="flex items-center gap-2 rounded-md px-3 py-2 text-left text-sm text-red-500 transition hover:bg-red-50 dark:hover:bg-red-950/20 disabled:opacity-50">
                            <Trash2 className="h-3.5 w-3.5" /> Delete
                          </button>
                        </div>
                      </PopoverContent>
                    </Popover>
                  </div>

                  {/* Icon */}
                  <div className="mt-4 flex justify-center">
                    <div className={`flex h-14 w-14 items-center justify-center overflow-hidden rounded-xl ${isActive ? 'bg-accent/8' : 'bg-bg-hover'}`}>
                      <ProfileIcon icon={profile.icon} name={profile.name} />
                    </div>
                  </div>

                  {/* Name & Pack */}
                  <div className="mt-3 text-center">
                    <h3 className="text-sm font-semibold text-text-main">{profile.name}</h3>
                    <p className="mt-0.5 text-xs text-text-muted">{packLabel(basePack, profile.base_pack)}</p>
                  </div>

                  {/* Status */}
                  <div className="mt-2 min-h-[16px] text-center">
                    {issues.length > 0 ? (
                      <span className={`text-[11px] ${hasDanger ? 'text-red-500' : 'text-amber-500'}`}>{issues[0].description}</span>
                    ) : profileReady ? (
                      <Badge variant="success" className="text-[10px]">Ready</Badge>
                    ) : null}
                  </div>

                  {/* Actions - primary right */}
                  <div className="mt-auto pt-4 flex gap-2 justify-end">
                    <Button variant="outline" size="sm" onClick={() => patchSearchParams({ edit: profile.profile_id })}>Edit</Button>
                    <Button size="sm" onClick={() => void handleLaunch(profile.profile_id)} disabled={!profileReady || busy} loading={actionState?.type === 'launch' && busy}>
                      <Rocket className="h-3.5 w-3.5" /> Launch
                    </Button>
                  </div>
                </article>
              );
            })}

            {/* Create card */}
            <button
              onClick={openCreateDialog}
              className="flex flex-col items-center justify-center rounded-xl border border-dashed border-border bg-bg-card/50 p-5 text-center transition hover:border-accent/40 hover:bg-bg-card min-h-[240px]"
            >
              <div className="flex h-12 w-12 items-center justify-center rounded-xl border border-border bg-bg-hover text-text-muted">
                <Plus className="h-5 w-5" />
              </div>
              <h3 className="mt-3 text-sm font-semibold text-text-main">Create Profile</h3>
              <p className="mt-1 text-xs text-text-muted">Build a new startup profile</p>
            </button>
          </section>
        )}

        {/* Edit Panel */}
        {draft && catalog && <EditPanel
          draft={draft}
          setDraft={setDraft}
          catalog={catalog}
          catalogPacks={catalogPacks}
          selectedBasePack={selectedBasePack}
          availablePacksToAdd={availablePacksToAdd}
          isDirty={isDirty}
          actionState={actionState}
          profileCount={profileCount}
          editProfileId={editProfileId}
          patchSearchParams={patchSearchParams}
          handleSave={handleSave}
          handleActivate={handleActivate}
          handleDuplicate={handleDuplicate}
          handleDelete={handleDelete}
          handleLaunch={handleLaunch}
          handleOpenProfileGraph={handleOpenProfileGraph}
          handleOpenApiMap={handleOpenApiMap}
          handleApprovePack={handleApprovePack}
          handleAddPack={handleAddPack}
          handleRemovePack={handleRemovePack}
          handleOverrideChange={handleOverrideChange}
        />}
      </div>

      {createDialogOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-6 backdrop-blur-sm">
          <section
            aria-labelledby="create-profile-title"
            aria-modal="true"
            className="w-full max-w-md rounded-xl border border-border bg-bg-card p-6 shadow-xl"
            role="dialog"
          >
            <h2 id="create-profile-title" className="text-lg font-semibold text-text-main">Create Profile</h2>
            <p className="mt-2 text-sm text-text-muted">Choose a base pack before saving a new profile. Going back does not create anything.</p>
            <label className="mt-5 block text-xs font-medium uppercase tracking-wider text-text-muted">
              Profile name
              <input
                className="mt-1.5 w-full rounded-lg border border-border bg-bg-main px-3 py-2.5 text-sm text-text-main outline-none transition focus:border-accent focus:ring-2 focus:ring-[var(--ring-color)]"
                onChange={(event) => setNewProfileName(event.target.value)}
                placeholder="New Profile"
                value={newProfileName}
              />
            </label>
            <label className="mt-4 block text-xs font-medium uppercase tracking-wider text-text-muted">
              Base pack
              <select
                className="rumi-select mt-1.5 w-full rounded-lg border border-border px-3 py-2.5 pr-9 text-sm outline-none transition"
                onChange={(event) => setNewProfileBasePack(event.target.value)}
                value={newProfileBasePack}
              >
                <option value="">Choose a pack</option>
                {catalogPacks.map((pack: any) => (
                  <option disabled={!pack.available} key={pack.pack_id} value={pack.pack_id}>
                    {packLabel(pack)}{pack.available ? '' : ' (unavailable)'}
                  </option>
                ))}
              </select>
            </label>
            <div className="mt-6 flex justify-end gap-3">
              <Button disabled={actionState?.type === 'create'} onClick={() => setCreateDialogOpen(false)} variant="outline">Back</Button>
              <Button disabled={!newProfileBasePack || actionState?.type === 'create'} loading={actionState?.type === 'create'} onClick={() => void handleCreate()}>
                Create Profile
              </Button>
            </div>
          </section>
        </div>
      )}
    </div>
  );
}

function ProfileIcon({ icon, name }: { icon?: string | null; name: string }) {
  const [failed, setFailed] = useState(false);
  return (
    <img
      alt={`${name} icon`}
      className="h-full w-full object-cover p-2"
      onError={() => setFailed(true)}
      src={!failed && icon ? icon : '/panel/assets/tobkiri-launcher-icon.png'}
    />
  );
}

function SupervisorSnapshot({
  data,
  loading,
  error,
}: {
  data: DashboardData['supervisor'];
  loading: boolean;
  error: string | null;
}) {
  const router = data?.router ?? null;
  const metrics = data?.metrics ?? null;
  const defaultSandbox = data?.sandbox_providers.find((provider) => provider.default) ?? null;
  const localSandbox = data?.sandbox_providers.find((provider) => provider.id === 'local_packaged') ?? null;
  const selectedSession = data?.selected_session ?? null;
  const computerLayer = router?.fallback_layers.find((layer) => layer.id === 'computer_use') ?? null;
  const recentEvent = data?.recent_events[0] ?? null;
  const macDriverOrder = router?.computer_driver_order.darwin ?? [];
  const routeCount = (router?.operation_layers.length ?? 0) + (router?.fallback_layers.length ?? 0);
  const capabilities = data?.capabilities ?? null;

  if (!data) {
    return (
      <section className="rounded-xl border border-border bg-bg-card p-5">
        <div className="flex items-center gap-3">
          <Monitor className="h-4 w-4 text-text-muted" />
          <div className="min-w-0 flex-1">
            <h2 className="text-sm font-semibold text-text-main">Supervisor Snapshot</h2>
            <p className="text-xs text-text-muted">
              {loading ? 'Loading runtime snapshot...' : error || 'Runtime snapshot unavailable.'}
            </p>
          </div>
          {loading && <TobkiriLoadingMark />}
        </div>
      </section>
    );
  }

  return (
    <section className="grid gap-4 lg:grid-cols-[minmax(0,1.05fr)_minmax(0,0.95fr)_minmax(0,1fr)]">
      <article className="min-w-0 rounded-xl border border-border bg-bg-card p-5">
        <div className="flex items-start justify-between gap-3">
          <div className="flex items-center gap-2">
            <Route className="h-4 w-4 text-accent" />
            <h2 className="text-sm font-semibold text-text-main">Runtime Router</h2>
          </div>
          <Badge variant="secondary" className="text-[10px]">{formatRuntimeLabel(router?.policy)}</Badge>
        </div>
        <div className="mt-4 grid grid-cols-3 gap-2 text-center">
          <MetricTile label="Routes" value={String(routeCount || '--')} />
          <MetricTile label="Structured" value={String(router?.operation_layers.length ?? '--')} />
          <MetricTile label="Fallback" value={String(router?.fallback_layers.length ?? '--')} />
        </div>
        <div className="mt-4 space-y-2">
          <div className="flex items-center justify-between gap-3 text-xs">
            <span className="text-text-muted">First layer</span>
            <span className="truncate text-text-main">{router?.operation_layers[0]?.label ?? '--'}</span>
          </div>
          <div className="flex items-center justify-between gap-3 text-xs">
            <span className="text-text-muted">Last layer</span>
            <span className="truncate text-text-main">{computerLayer?.label ?? 'Computer use'}</span>
          </div>
          <div className="flex items-center gap-2 text-xs text-text-muted">
            <Terminal className="h-3.5 w-3.5" />
            <span className="truncate">{formatCompactList(macDriverOrder.slice(0, 4))}</span>
          </div>
        </div>
      </article>

      <article className="min-w-0 rounded-xl border border-border bg-bg-card p-5">
        <div className="flex items-start justify-between gap-3">
          <div className="flex items-center gap-2">
            <Cloud className="h-4 w-4 text-accent" />
            <h2 className="text-sm font-semibold text-text-main">Sandbox Providers</h2>
          </div>
          <Badge variant="success" className="text-[10px]">{defaultSandbox?.tier ?? 'default'}</Badge>
        </div>
        <div className="mt-4 space-y-3">
          <ProviderRow provider={defaultSandbox} />
          <ProviderRow provider={localSandbox} />
        </div>
        <div className="mt-4 flex items-center gap-2 text-xs text-text-muted">
          <ShieldCheck className="h-3.5 w-3.5" />
          <span className="truncate">{formatCompactList(data.security_guardrails.slice(0, 3))}</span>
        </div>
      </article>

      <article className="min-w-0 rounded-xl border border-border bg-bg-card p-5">
        <div className="flex items-start justify-between gap-3">
          <div className="flex items-center gap-2">
            <Monitor className="h-4 w-4 text-accent" />
            <h2 className="text-sm font-semibold text-text-main">Run Snapshot</h2>
          </div>
          <Badge variant={capabilities?.snapshot ? 'success' : 'secondary'} className="text-[10px]">
            {capabilities?.snapshot ? 'Snapshot' : 'Unavailable'}
          </Badge>
        </div>
        <div className="mt-4 grid grid-cols-4 gap-2 text-center">
          <MetricTile label="Active" value={String(metrics?.active_runs ?? 0)} />
          <MetricTile label="Approval" value={String(metrics?.waiting_approvals ?? 0)} />
          <MetricTile label="Stale" value={String(metrics?.stale_runs ?? 0)} />
          <MetricTile label="Failed" value={String(metrics?.failed_runs ?? 0)} />
        </div>
        <div className="mt-4 space-y-2 text-xs">
          <div className="flex items-center justify-between gap-3">
            <span className="text-text-muted">Run</span>
            <span className="truncate text-text-main">{selectedSession?.run_id ?? 'No run snapshot'}</span>
          </div>
          <div className="flex items-center justify-between gap-3">
            <span className="text-text-muted">Last event</span>
            <span className="truncate text-text-main">{recentEvent?.event_type ?? '--'}</span>
          </div>
          <CapabilityRow label="Live screen" enabled={capabilities?.live_screen === true} />
          <CapabilityRow label="Takeover" enabled={capabilities?.takeover === true} />
          <CapabilityRow label="Replay" enabled={capabilities?.replay === true} />
        </div>
      </article>
    </section>
  );
}

function MetricTile({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-lg border border-border bg-bg-hover/40 px-2 py-2">
      <div className="text-[11px] text-text-muted">{label}</div>
      <div className="mt-1 text-sm font-semibold text-text-main">{value}</div>
    </div>
  );
}

function CapabilityRow({ label, enabled }: { label: string; enabled: boolean }) {
  return (
    <div className="flex items-center justify-between gap-3">
      <span className="text-text-muted">{label}</span>
      <span className={enabled ? 'truncate text-text-main' : 'truncate text-text-muted'}>
        {enabled ? 'Available' : 'Not available'}
      </span>
    </div>
  );
}

function ProviderRow({ provider }: { provider: NonNullable<DashboardData['supervisor']>['sandbox_providers'][number] | null }) {
  if (!provider) {
    return null;
  }
  return (
    <div className="flex items-start justify-between gap-3 rounded-lg border border-border bg-bg-hover/40 px-3 py-2">
      <div className="min-w-0">
        <div className="truncate text-xs font-medium text-text-main">{provider.label}</div>
        <div className="mt-0.5 truncate text-[11px] text-text-muted">{formatCompactList(provider.providers.slice(0, 4))}</div>
      </div>
      <Badge variant={provider.default ? 'success' : 'secondary'} className="shrink-0 text-[10px]">
        {provider.default ? 'Default' : formatRuntimeLabel(provider.user_burden)}
      </Badge>
    </div>
  );
}

function formatRuntimeLabel(value: string | undefined): string {
  if (!value) return '--';
  return value.replaceAll('_', ' ');
}

function formatCompactList(values: string[]): string {
  if (!values.length) return '--';
  return values.map(formatRuntimeLabel).join(' / ');
}

// --- Edit Panel (extracted for readability) ---

interface EditPanelProps {
  draft: ApiStartupProfile;
  setDraft: (d: ApiStartupProfile) => void;
  catalog: any;
  catalogPacks: any[];
  selectedBasePack: any;
  availablePacksToAdd: any[];
  isDirty: boolean;
  actionState: { profileId?: string; type: string } | null;
  profileCount: number;
  editProfileId: string | null;
  patchSearchParams: (u: Record<string, string | null>) => void;
  handleSave: () => Promise<void>;
  handleActivate: (id: string) => Promise<void>;
  handleDuplicate: (id: string) => Promise<void>;
  handleDelete: (id: string, name: string) => void;
  handleLaunch: (id: string) => Promise<void>;
  handleOpenProfileGraph: (id: string) => void;
  handleOpenApiMap: (id: string) => void;
  handleApprovePack: (packId: string) => Promise<void>;
  handleAddPack: (packId: string) => Promise<void>;
  handleRemovePack: (packId: string) => Promise<void>;
  handleOverrideChange: (portKey: string, nodeId: string) => Promise<void>;
}

function EditPanel({
  draft, setDraft, catalog, catalogPacks, selectedBasePack, availablePacksToAdd,
  isDirty, actionState, profileCount, editProfileId, patchSearchParams,
  handleSave, handleActivate, handleDuplicate, handleDelete, handleLaunch,
  handleOpenProfileGraph, handleOpenApiMap,
  handleApprovePack,
  handleAddPack, handleRemovePack, handleOverrideChange,
}: EditPanelProps) {
  const [activeSection, setActiveSection] = useState<'general' | 'packs' | 'wiring'>('general');
  const [compilePreview, setCompilePreview] = useState<StartupProfileCompilePreviewResponseData | null>(null);
  const [compilePreviewError, setCompilePreviewError] = useState<string | null>(null);
  const [compilePreviewLoading, setCompilePreviewLoading] = useState(false);
  const compilePreviewKey = useMemo(
    () => JSON.stringify({
      profile_id: draft.profile_id,
      base_pack: draft.base_pack,
      graph_id: draft.graph_id,
      packs: draft.packs,
      node_overrides: draft.node_overrides,
      launch_capability_graph: draft.launch_capability_graph,
      capability_profile_id: draft.capability_profile_id,
      surfaces: draft.surfaces,
    }),
    [draft],
  );

  useEffect(() => {
    let cancelled = false;
    setCompilePreviewLoading(true);
    setCompilePreviewError(null);
    const timeout = window.setTimeout(() => {
      void compileStartupProfilePreview(draft.profile_id, draft)
        .then((preview) => {
          if (cancelled) return;
          setCompilePreview(preview);
        })
        .catch((error) => {
          if (cancelled) return;
          setCompilePreview(null);
          setCompilePreviewError(error instanceof Error ? error.message : 'Compile preview failed.');
        })
        .finally(() => {
          if (!cancelled) setCompilePreviewLoading(false);
        });
    }, 300);
    return () => {
      cancelled = true;
      window.clearTimeout(timeout);
    };
  }, [draft.profile_id, compilePreviewKey]);

  const launchTarget = compilePreview?.surface_launch_target ?? compilePreview?.capability_graph?.surface_launch_target ?? null;
  const previewDiagnostics = compilePreview?.diagnostics ?? compilePreview?.capability_graph?.diagnostics ?? [];
  const firstPreviewError = previewDiagnostics.find((item) => item.level === 'error')?.message ?? null;

  return (
    <section
      className="fixed inset-0 z-50 flex flex-col overflow-hidden bg-bg-main"
      aria-label={`Edit ${draft.name}`}
    >
      {/* Edit header */}
      <div className="flex shrink-0 flex-col gap-4 border-b border-border bg-bg-card px-6 py-4 lg:flex-row lg:items-end lg:justify-between">
        <div className="space-y-2">
          <button
            onClick={() => patchSearchParams({ edit: null })}
            className="inline-flex items-center gap-1.5 text-sm text-text-muted transition hover:text-text-main"
          >
            <ArrowLeft className="h-3.5 w-3.5" /> Back to profiles
          </button>
          <div>
            <h2 className="text-xl font-semibold tracking-tight text-text-main">{draft.name}</h2>
            <p className="mt-0.5 text-sm text-text-muted">Edit profile settings, packs, and graph port overrides.</p>
          </div>
        </div>

        {/* Actions - destructive separated left, primary right */}
        <div className="flex flex-wrap gap-2">
          <Button variant="outline" size="sm" onClick={() => handleOpenProfileGraph(draft.profile_id)}>
            <Share2 className="h-3.5 w-3.5" /> Open Profile Graph
          </Button>
          <Button variant="outline" size="sm" onClick={() => handleOpenApiMap(draft.profile_id)}>
            <Route className="h-3.5 w-3.5" /> Open API Map
          </Button>
          <Button variant="outline" size="sm" onClick={() => void handleDuplicate(draft.profile_id)} disabled={actionState?.profileId === draft.profile_id}>
            <Copy className="h-3.5 w-3.5" /> Duplicate
          </Button>
          <Button variant="destructive" size="sm" onClick={() => handleDelete(draft.profile_id, draft.name)} disabled={profileCount <= 1 || actionState?.profileId === draft.profile_id}>
            <Trash2 className="h-3.5 w-3.5" /> Delete
          </Button>
          <div className="flex-1" />
          <Button size="sm" onClick={() => void handleLaunch(draft.profile_id)} disabled={actionState?.profileId === draft.profile_id} loading={actionState?.type === 'launch' && actionState?.profileId === draft.profile_id}>
            <Rocket className="h-3.5 w-3.5" /> Launch
          </Button>
          <Button size="sm" onClick={handleSave} disabled={!isDirty || actionState?.type === 'save'} loading={actionState?.type === 'save'}>
            <Save className="h-3.5 w-3.5" /> Save
          </Button>
        </div>
      </div>

      {/* Edit body */}
      <div className="mx-auto grid min-h-0 w-full max-w-[1400px] flex-1 gap-6 overflow-y-auto px-6 py-6 xl:grid-cols-[minmax(0,1fr)_300px]">
        <div className="grid min-w-0 gap-5 lg:grid-cols-[190px_minmax(0,1fr)]">
          <nav aria-label="Profile settings" className="space-y-1 lg:sticky lg:top-0 lg:self-start">
            {([
              ['general', 'General', 'Name, icon, and base pack'],
              ['packs', 'Packs', 'Installed packs for this profile'],
              ['wiring', 'Startup wiring', 'Launch graph port overrides'],
            ] as const).map(([section, label, description]) => (
              <button
                aria-current={activeSection === section ? 'page' : undefined}
                className={`w-full rounded-lg px-3 py-2.5 text-left transition ${
                  activeSection === section
                    ? 'bg-accent/10 text-accent'
                    : 'text-text-muted hover:bg-bg-hover hover:text-text-main'
                }`}
                key={section}
                onClick={() => setActiveSection(section)}
                type="button"
              >
                <span className="block text-sm font-medium">{label}</span>
                <span className="mt-0.5 block text-[11px] leading-4 opacity-75">{description}</span>
              </button>
            ))}
          </nav>
          <div className="min-w-0">
          {/* General settings */}
          {activeSection === 'general' ? <div className="rounded-lg border border-border bg-bg-main p-5">
            <h3 className="text-sm font-semibold text-text-main">General settings</h3>
            <p className="mt-0.5 text-xs text-text-muted">Profile name and base pack.</p>
            <div className="mt-4 grid gap-4 sm:grid-cols-2">
              <div className="space-y-1.5">
                <label className="text-xs font-medium uppercase tracking-wider text-text-muted">Profile name</label>
                <input
                  value={draft.name}
                  onChange={(e) => setDraft({ ...draft, name: e.target.value })}
                  className="w-full rounded-lg border border-border bg-bg-hover px-3 py-2.5 text-sm text-text-main outline-none transition focus:border-accent focus:ring-2 focus:ring-[var(--ring-color)]"
                />
              </div>
              <div className="space-y-1.5">
                <label className="text-xs font-medium uppercase tracking-wider text-text-muted">Base pack</label>
                <select
                  value={draft.base_pack}
                  onChange={(e) => {
                    const nextBasePack = e.target.value;
                    const nextPack = catalogPacks.find((pack: any) => pack.pack_id === nextBasePack);
                    setDraft({
                      ...draft,
                      base_pack: nextBasePack,
                      graph_id: nextPack?.graphs[0]?.graph_id ?? draft.graph_id,
                      packs: draft.packs.includes(nextBasePack) ? draft.packs : [nextBasePack, ...draft.packs],
                    });
                  }}
                  className="rumi-select w-full rounded-lg border border-border px-3 py-2.5 pr-9 text-sm outline-none transition"
                >
                  {catalogPacks.map((pack: any) => (
                    <option key={pack.pack_id} value={pack.pack_id} disabled={!pack.available}>
                      {packLabel(pack)} {pack.available ? '' : '(unavailable)'}
                    </option>
                  ))}
                </select>
              </div>
            </div>

            <div className="mt-4 space-y-1.5">
              <label className="text-xs font-medium uppercase tracking-wider text-text-muted">Profile icon</label>
              <div className="flex items-center gap-3">
                <div className="flex h-11 w-11 shrink-0 items-center justify-center overflow-hidden rounded-lg border border-border bg-bg-hover">
                  <ProfileIcon icon={draft.icon} name={draft.name} />
                </div>
                <input
                  value={draft.icon ?? ''}
                  onChange={(event) => setDraft({ ...draft, icon: event.target.value || null })}
                  placeholder="Optional HTTPS image URL"
                  className="w-full rounded-lg border border-border bg-bg-hover px-3 py-2.5 text-sm text-text-main outline-none transition focus:border-accent focus:ring-2 focus:ring-[var(--ring-color)]"
                />
              </div>
              <p className="text-xs text-text-muted">Leave blank to use the Tobkiri icon.</p>
            </div>

            {selectedBasePack && !selectedBasePack.available && (
              <div className="mt-4 rounded-lg border border-amber-200 bg-amber-50 p-3 dark:border-amber-900/30 dark:bg-amber-950/20">
                <div className="flex items-center gap-2 text-sm font-medium text-amber-700 dark:text-amber-300">
                  <AlertCircle className="h-4 w-4" /> Base pack needs attention
                </div>
                <ul className="mt-2 space-y-1 text-xs text-text-muted">
                  {selectedBasePack.approval_issues.map((issue: string) => (
                    <li key={issue}>{describeStartupIssue(issue, packLabel(selectedBasePack)).description}</li>
                  ))}
                </ul>
                <Button
                  className="mt-3"
                  size="sm"
                  onClick={() => void handleApprovePack(selectedBasePack.pack_id)}
                  disabled={actionState?.type === 'approve'}
                  loading={actionState?.type === 'approve'}
                >
                  <ShieldCheck className="mr-2 h-3.5 w-3.5" /> Approve {packLabel(selectedBasePack)}
                </Button>
              </div>
            )}
          </div> : null}

          {/* Profile packs */}
          {activeSection === 'packs' ? <div className="rounded-lg border border-border bg-bg-main p-5">
            <h3 className="text-sm font-semibold text-text-main">Profile packs</h3>
            <p className="mt-0.5 text-xs text-text-muted">Add packs before using their nodes as overrides.</p>
            <div className="mt-4 space-y-3">
              {draft.packs.map((packId: string) => {
                const pack = catalogPacks.find((item: any) => item.pack_id === packId) ?? null;
                const canRemove = packId !== draft.base_pack;
                return (
                  <div key={packId} className="flex items-center justify-between gap-3 rounded-lg border border-border bg-bg-hover/50 p-3">
                    <div className="min-w-0">
                      <div className="truncate text-sm font-medium text-text-main">{packLabel(pack, packId)}</div>
                      <div className="truncate text-xs text-text-muted">{packId}</div>
                    </div>
                    <div className="flex shrink-0 gap-2">
                      {pack && !pack.available ? (
                        <Button
                          size="sm"
                          onClick={() => void handleApprovePack(packId)}
                          disabled={actionState?.type === 'approve'}
                          loading={actionState?.type === 'approve'}
                        >
                          Approve
                        </Button>
                      ) : null}
                      <Button variant="outline" size="sm" onClick={() => void handleRemovePack(packId)} disabled={!canRemove || actionState?.profileId === draft.profile_id}>
                        {packId === draft.base_pack ? 'Base' : 'Remove'}
                      </Button>
                    </div>
                  </div>
                );
              })}
              <div className="space-y-1.5">
                <label className="text-xs font-medium uppercase tracking-wider text-text-muted">Add pack</label>
                <select
                  value=""
                  onChange={(e) => void handleAddPack(e.target.value)}
                  disabled={availablePacksToAdd.length === 0 || actionState?.profileId === draft.profile_id}
                  className="rumi-select w-full rounded-lg border border-border px-3 py-2.5 pr-9 text-sm outline-none transition disabled:opacity-60"
                >
                  <option value="">Select a pack</option>
                  {availablePacksToAdd.map((pack: any) => (
                    <option key={pack.pack_id} value={pack.pack_id}>{packLabel(pack)}</option>
                  ))}
                </select>
              </div>
            </div>
          </div> : null}

          {/* Graph port overrides */}
          {activeSection === 'wiring' ? <div className="rounded-lg border border-border bg-bg-main p-5">
            <h3 className="text-sm font-semibold text-text-main">Graph port overrides</h3>
            <p className="mt-0.5 text-xs text-text-muted">Override graph inputs with compatible nodes.</p>
            <div className="mt-4 space-y-3">
              {draft.graph_ports.map((graphPort: any) => {
                const compatibleNodes = compatibleNodesForPort(catalog, draft, graphPort);
                const currentOverride = draft.node_overrides[graphPort.port_key] ?? '';
                const defaultNode = graphPort.source_node_ref || graphPort.source_ref;
                return (
                  <div key={graphPort.port_key} className="rounded-lg border border-border bg-bg-hover/50 p-3">
                    <div className="flex flex-col gap-1 sm:flex-row sm:items-center sm:justify-between">
                      <div>
                        <div className="text-sm font-medium text-text-main">{titleCasePortKey(graphPort.port_key)}</div>
                        <div className="text-xs text-text-muted">Default: {defaultNode}</div>
                      </div>
                      <Badge variant="outline" className="text-[10px] w-fit">
                        {(graphPort.target_port?.standards ?? graphPort.target_port?.contracts ?? []).join(', ') || graphPort.port_key}
                      </Badge>
                    </div>
                    <select
                      value={currentOverride}
                      onChange={(e) => void handleOverrideChange(graphPort.port_key, e.target.value)}
                      disabled={actionState?.profileId === draft.profile_id}
                      className="rumi-select mt-2 w-full rounded-lg border border-border px-3 py-2 pr-9 text-sm outline-none transition"
                    >
                      <option value="">Use graph default ({defaultNode})</option>
                      {compatibleNodes.map((node: any) => (
                        <option key={node.node_id} value={node.node_id}>{node.node_id}</option>
                      ))}
                    </select>
                    {compatibleNodes.length === 0 && (
                      <div className="mt-2 flex items-start gap-2 rounded-lg border border-amber-200 bg-amber-50 p-2 text-xs text-amber-700 dark:border-amber-900/30 dark:bg-amber-950/20 dark:text-amber-300">
                        <AlertCircle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                        <span>No compatible nodes available from this profile's packs.</span>
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          </div> : null}
          </div>
        </div>

        {/* Sidebar metadata */}
        <aside className="space-y-4">
          <div className="rounded-lg border border-border bg-bg-main p-4">
            <div className="flex items-center justify-between gap-3">
              <div className="text-xs font-medium uppercase tracking-wider text-text-muted">Launch target</div>
              {compilePreviewLoading ? (
                <TobkiriLoadingMark />
              ) : compilePreview?.ok ? (
                <Badge variant="success" className="text-[10px]">Ready</Badge>
              ) : (
                <Badge variant="warning" className="text-[10px]">Check</Badge>
              )}
            </div>
            <div className="mt-3 space-y-2 text-sm">
              {launchTarget ? (
                <>
                  <div>
                    <div className="text-text-muted text-xs">Pack</div>
                    <div className="mt-1 break-all rounded-md border border-border bg-bg-hover px-2.5 py-1.5 font-mono text-xs text-text-main">{launchTarget.pack_id}</div>
                  </div>
                  {launchTarget.node_id && (
                    <div>
                      <div className="text-text-muted text-xs">Node</div>
                      <div className="mt-1 break-all rounded-md border border-border bg-bg-hover px-2.5 py-1.5 font-mono text-xs text-text-main">{launchTarget.node_id}</div>
                    </div>
                  )}
                  {launchTarget.surface && (
                    <div>
                      <div className="text-text-muted text-xs">Surface</div>
                      <div className="mt-0.5 text-text-main text-xs">{launchTarget.surface}</div>
                    </div>
                  )}
                </>
              ) : (
                <div className="rounded-md border border-border bg-bg-hover px-2.5 py-2 text-xs text-text-muted">
                  {compilePreviewError || firstPreviewError || 'No launch target resolved.'}
                </div>
              )}
            </div>
          </div>
          <div className="rounded-lg border border-border bg-bg-main p-4">
            <div className="text-xs font-medium uppercase tracking-wider text-text-muted">Metadata</div>
            <div className="mt-3 space-y-3 text-sm">
              <div>
                <div className="text-text-muted text-xs">Profile ID</div>
                <div className="mt-1 break-all rounded-md border border-border bg-bg-hover px-2.5 py-1.5 font-mono text-xs text-text-main">{draft.profile_id}</div>
              </div>
              <div>
                <div className="text-text-muted text-xs">Created</div>
                <div className="mt-0.5 text-text-main text-xs">{formatTimestamp(draft.created_at)}</div>
              </div>
              <div>
                <div className="text-text-muted text-xs">Last updated</div>
                <div className="mt-0.5 text-text-main text-xs">{formatTimestamp(draft.updated_at)}</div>
              </div>
              <div>
                <div className="text-text-muted text-xs">Status</div>
                <div className="mt-0.5 text-text-main text-xs">{isDirty ? 'Unsaved changes' : 'Saved'}</div>
              </div>
            </div>
          </div>
        </aside>
      </div>
    </section>
  );
}

// --- Skeleton ---

function DashboardSkeleton() {
  return <TobkiriLoader label="Loading Tobkiri home..." />;
}
