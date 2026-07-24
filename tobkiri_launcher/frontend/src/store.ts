import { create } from 'zustand';
import {
  checkHealth,
  fetchDashboard,
  approvePack as apiApprovePack,
  fetchPacks,
  fetchFlows,
  fetchProfile,
  fetchVersion,
  enablePack as apiEnablePack,
  disablePack as apiDisablePack,
  createFlow as apiCreateFlow,
  updateFlow as apiUpdateFlow,
  deleteFlow as apiDeleteFlow,
  updateProfile as apiUpdateProfile,
  restartKernel as apiRestartKernel,
  fetchUpdates,
  fetchUpdateSettings,
  updateUpdateSettings,
  applyUpdate as apiApplyUpdate,
  openExternalUrl,
  startOAuth,
} from './lib/api';
import type { ApiSupervisorDashboard } from './lib/apiTypes';
import {
  transformDashboard,
  transformPacks,
  transformFlows,
  transformProfile,
  transformVersion,
} from './lib/transforms';
import { RUMI_DISPLAY_VERSION } from './lib/version';
import {
  COLOR_MODE_STORAGE_KEY,
  THEME_STORAGE_KEY,
  normalizeColorMode,
  normalizeTheme,
} from './lib/appearance';
import type { ColorMode, Theme } from './lib/appearance';
import { AVATAR_OPTIONS, DEFAULT_AVATAR } from './lib/avatar';

export type { ColorMode, Theme } from './lib/appearance';
export { AVATAR_OPTIONS } from './lib/avatar';

function readLocalStorage(key: string): string | null {
  try {
    if (typeof localStorage === 'undefined' || typeof localStorage.getItem !== 'function') {
      return null;
    }
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

function writeLocalStorage(key: string, value: string): void {
  try {
    if (typeof localStorage === 'undefined' || typeof localStorage.setItem !== 'function') {
      return;
    }
    localStorage.setItem(key, value);
  } catch {
    // Ignore storage failures in non-browser contexts.
  }
}

export interface Toast {
  id: string;
  message: string;
  type: 'success' | 'error';
}

export interface DialogConfig {
  title: string;
  message: string;
  onConfirm: () => void | Promise<void>;
  confirmText?: string;
  confirmPendingText?: string;
  cancelText?: string;
}

export interface Pack {
  id: string;
  name: string;
  version: string;
  type: 'core' | 'community';
  enabled: boolean;
  description: string;
  approvalStatus: string;
  approvalReason: string | null;
  approved: boolean;
  hashValid: boolean | null;
  criticalChanged: boolean | null;
  approvalIssues: string[];
  capabilities: { name: string; description: string }[];
  flows: string[];
  dependencies: string[];
}

export interface Flow {
  id: string;
  name: string;
  content: string;
}

export interface Activity {
  id: number;
  timestamp: string;
  type: 'kernel_start' | 'pack_load' | 'flow_success' | 'flow_fail' | 'error';
  message: string;
}

export interface DashboardData {
  kernelStatus: 'running' | 'stopped' | 'error';
  uptime: string;
  activePacks: number;
  registeredFlows: number;
  activities: Activity[];
  supervisor: ApiSupervisorDashboard | null;
}

export interface Profile {
  avatar: string;
  username: string;
  language: string;
  job: string;
  connected: boolean;
}

export interface VersionInfo {
  app: string;
  kernel: string;
  python: string;
  launcher: string;
  docker: {
    installed: boolean;
    version: string;
    type: string;
  };
}

export type UpdateTarget = 'tobkiri' | 'defaultspack';

export interface UpdateInfo {
  target: UpdateTarget;
  currentVersion: string;
  latestVersion: string;
  updateAvailable: boolean;
  releaseUrl: string;
  repo: string;
}

export type RuntimeStatus = 'starting' | 'panel_ready' | 'runtime_ready' | 'error';
const SIDEBAR_STORAGE_KEY = 'tobkiri-launcher-sidebar-open';
const LEGACY_SIDEBAR_STORAGE_KEY = 'rumi-viewer-sidebar-open';
const SETUP_STORAGE_KEY = 'tobkiri-launcher-setup';
const LEGACY_SETUP_STORAGE_KEY = 'rumi-setup';
const ADVANCED_PROFILE_STORAGE_KEY = 'tobkiri-launcher-advanced-profile';

interface AppState {
  theme: Theme;
  setTheme: (theme: Theme) => void;

  colorMode: ColorMode;
  setColorMode: (mode: ColorMode) => void;

  isSetupDone: boolean;
  setSetupDone: (done: boolean) => void;

  isSidebarOpen: boolean;
  setSidebarOpen: (open: boolean) => void;

  selectedStartupProfileId: string;
  setSelectedStartupProfileId: (profileId: string) => void;

  toasts: Toast[];
  addToast: (message: string, type: 'success' | 'error') => void;
  removeToast: (id: string) => void;

  dialog: DialogConfig | null;
  showDialog: (config: DialogConfig) => void;
  closeDialog: () => void;

  isLoading: boolean;
  apiError: string | null;

  runtimeReady: boolean;
  runtimeStatus: RuntimeStatus;
  runtimeError: string | null;
  runtimeDisconnected: boolean;
  lastRuntimeHealthyAt: number | null;
  setRuntimeHealth: (health: { status?: 'ok' | 'error'; panel_ready?: boolean; runtime_ready?: boolean; runtime_status?: RuntimeStatus; runtime_error?: string | null }) => void;
  refreshRuntimeHealth: () => Promise<void>;

  packs: Pack[];
  packsLoading: boolean;
  packsError: string | null;
  packTogglePending: Record<string, boolean>;
  loadPacks: () => Promise<void>;
  approvePack: (id: string) => Promise<void>;
  togglePack: (id: string) => Promise<boolean>;

  flows: Flow[];
  flowsLoading: boolean;
  flowsError: string | null;
  loadFlows: () => Promise<boolean>;
  addFlow: (flow: { id: string; name: string; content: string }) => Promise<boolean>;
  updateFlow: (id: string, content: string) => Promise<boolean>;
  deleteFlow: (id: string) => Promise<boolean>;

  dashboard: DashboardData;
  loadDashboard: () => Promise<void>;
  setKernelStatus: (status: 'running' | 'stopped' | 'error') => void;
  restartKernel: () => Promise<void>;

  profile: Profile;
  loadProfile: () => Promise<void>;
  updateProfile: (profile: Partial<Profile>) => Promise<boolean>;
  connectAccount: () => Promise<void>;

  version: VersionInfo;
  loadVersion: () => Promise<void>;
  updates: UpdateInfo[];
  autoUpdate: Record<UpdateTarget, boolean>;
  updatesLoading: boolean;
  updateSettingsLoading: boolean;
  updateApplyingTarget: UpdateTarget | null;
  loadUpdates: () => Promise<void>;
  loadUpdateSettings: () => Promise<void>;
  setAutoUpdate: (target: UpdateTarget, enabled: boolean) => Promise<void>;
  applyUpdate: (target: UpdateTarget) => Promise<void>;
}

const defaultDashboard: DashboardData = {
  kernelStatus: 'stopped',
  uptime: '--',
  activePacks: 0,
  registeredFlows: 0,
  activities: [],
  supervisor: null,
};

const defaultProfile: Profile = {
  avatar: DEFAULT_AVATAR,
  username: 'User',
  language: 'en',
  job: '',
  connected: false,
};

const defaultVersion: VersionInfo = {
  app: RUMI_DISPLAY_VERSION,
  kernel: '--',
  python: '--',
  launcher: '--',
  docker: {
    installed: false,
    version: '',
    type: '',
  },
};

function transformUpdateInfo(update: {
  target: UpdateTarget;
  current_version: string;
  latest_version: string;
  update_available: boolean;
  release_url: string;
  repo: string;
}): UpdateInfo {
  return {
    target: update.target,
    currentVersion: update.current_version,
    latestVersion: update.latest_version,
    updateAvailable: update.update_available,
    releaseUrl: update.release_url,
    repo: update.repo,
  };
}

let packsLoadPromise: Promise<void> | null = null;
let flowsLoadPromise: Promise<boolean> | null = null;
let flowMutationVersion = 0;
const packMutationVersions = new Map<string, number>();

export const useAppStore = create<AppState>((set, get) => ({
  theme: normalizeTheme(readLocalStorage(THEME_STORAGE_KEY)),
  setTheme: (theme) => {
    writeLocalStorage(THEME_STORAGE_KEY, theme);
    set({ theme });
  },

  colorMode: normalizeColorMode(readLocalStorage(COLOR_MODE_STORAGE_KEY)),
  setColorMode: (mode) => {
    writeLocalStorage(COLOR_MODE_STORAGE_KEY, mode);
    set({ colorMode: mode });
  },

  isSetupDone:
    (readLocalStorage(SETUP_STORAGE_KEY) ?? readLocalStorage(LEGACY_SETUP_STORAGE_KEY)) === 'true',
  setSetupDone: (done) => {
    writeLocalStorage(SETUP_STORAGE_KEY, String(done));
    set({ isSetupDone: done });
  },

  isSidebarOpen: (readLocalStorage(SIDEBAR_STORAGE_KEY) ?? readLocalStorage(LEGACY_SIDEBAR_STORAGE_KEY)) !== 'false',
  setSidebarOpen: (open) => {
    writeLocalStorage(SIDEBAR_STORAGE_KEY, String(open));
    set({ isSidebarOpen: open });
  },

  selectedStartupProfileId: readLocalStorage(ADVANCED_PROFILE_STORAGE_KEY) ?? '',
  setSelectedStartupProfileId: (profileId) => {
    writeLocalStorage(ADVANCED_PROFILE_STORAGE_KEY, profileId);
    set({ selectedStartupProfileId: profileId });
  },

  toasts: [],
  addToast: (message, type) => {
    const id = Math.random().toString(36).substring(2, 9);
    set((state) => ({ toasts: [...state.toasts, { id, message, type }] }));
    setTimeout(() => {
      set((state) => ({ toasts: state.toasts.filter((t) => t.id !== id) }));
    }, 3000);
  },
  removeToast: (id) => set((state) => ({ toasts: state.toasts.filter((t) => t.id !== id) })),

  dialog: null,
  showDialog: (config) => set({ dialog: config }),
  closeDialog: () => set({ dialog: null }),

  isLoading: false,
  apiError: null,

  runtimeReady: false,
  runtimeStatus: 'starting',
  runtimeError: null,
  runtimeDisconnected: false,
  lastRuntimeHealthyAt: null,
  setRuntimeHealth: (health) =>
    set((state) => ({
      runtimeReady: Boolean(health.runtime_ready),
      runtimeStatus:
        health.runtime_status ??
        (health.status === 'error'
          ? 'error'
          : health.runtime_ready
            ? 'runtime_ready'
            : health.panel_ready
              ? 'panel_ready'
              : state.runtimeStatus),
      runtimeError: health.runtime_error ?? null,
      runtimeDisconnected: false,
      lastRuntimeHealthyAt: health.runtime_ready ? Date.now() : state.lastRuntimeHealthyAt,
    })),
  refreshRuntimeHealth: async () => {
    try {
      const health = await checkHealth();
      get().setRuntimeHealth(health);
    } catch (e) {
      const msg = e instanceof Error ? e.message : 'Failed to read runtime health';
      set((state) => ({
        runtimeReady: false,
        runtimeStatus: 'error',
        runtimeError: msg,
        runtimeDisconnected: state.lastRuntimeHealthyAt !== null,
      }));
    }
  },

  // ============================================================
  // Packs
  // ============================================================

  packs: [],
  packsLoading: false,
  packsError: null,
  packTogglePending: {},

  loadPacks: () => {
    if (packsLoadPromise) return packsLoadPromise;
    const versionsAtStart = new Map(packMutationVersions);
    set({ packsLoading: true, packsError: null });
    packsLoadPromise = (async () => {
      try {
        const data = await fetchPacks();
        const latestState = get();
        const currentById = new Map(latestState.packs.map((pack) => [pack.id, pack]));
        const packs = transformPacks(data.packs).map((pack) => {
          const before = versionsAtStart.get(pack.id) ?? 0;
          const after = packMutationVersions.get(pack.id) ?? 0;
          if (before === after && !latestState.packTogglePending[pack.id]) return pack;
          const current = currentById.get(pack.id);
          return current ? { ...pack, enabled: current.enabled } : pack;
        });
        set({ packs, packsError: null });
      } catch (e) {
        const msg = e instanceof Error ? e.message : 'Failed to load packs';
        set({ packsError: msg });
        get().addToast(msg, 'error');
      }
    })().finally(() => {
      packsLoadPromise = null;
      set({ packsLoading: false });
    });
    return packsLoadPromise;
  },

  approvePack: async (id) => {
    try {
      await apiApprovePack(id);
      await get().loadPacks();
      get().addToast('Pack approved.', 'success');
    } catch (e) {
      const msg = e instanceof Error ? e.message : 'Failed to approve pack';
      get().addToast(msg, 'error');
      throw e;
    }
  },

  togglePack: async (id) => {
    const state = get();
    const pack = state.packs.find((candidate) => candidate.id === id);
    if (!pack || state.packTogglePending[id]) return false;

    const version = (packMutationVersions.get(id) ?? 0) + 1;
    packMutationVersions.set(id, version);
    set((current) => ({
      packs: current.packs.map((candidate) => (
        candidate.id === id ? { ...candidate, enabled: !pack.enabled } : candidate
      )),
      packTogglePending: { ...current.packTogglePending, [id]: true },
    }));

    try {
      const response = pack.enabled
        ? await apiDisablePack(id)
        : await apiEnablePack(id);
      if (packMutationVersions.get(id) === version) {
        set((current) => ({
          packs: current.packs.map((candidate) => (
            candidate.id === id ? { ...candidate, enabled: response.enabled } : candidate
          )),
        }));
      }
      return true;
    } catch (e) {
      if (packMutationVersions.get(id) === version) {
        set((current) => ({
          packs: current.packs.map((candidate) => (
            candidate.id === id ? { ...candidate, enabled: pack.enabled } : candidate
          )),
        }));
      }
      const msg = e instanceof Error ? e.message : 'Failed to toggle pack';
      get().addToast(msg, 'error');
      return false;
    } finally {
      set((current) => {
        const pending = { ...current.packTogglePending };
        delete pending[id];
        return { packTogglePending: pending };
      });
    }
  },

  // ============================================================
  // Flows
  // ============================================================

  flows: [],
  flowsLoading: false,
  flowsError: null,

  loadFlows: () => {
    if (flowsLoadPromise) return flowsLoadPromise;
    const versionAtStart = flowMutationVersion;
    set({ flowsLoading: true, flowsError: null });
    flowsLoadPromise = (async () => {
      try {
        const data = await fetchFlows();
        if (versionAtStart === flowMutationVersion) {
          set({ flows: transformFlows(data.flows), flowsError: null });
        }
        return true;
      } catch (e) {
        const msg = e instanceof Error ? e.message : 'Failed to load flows';
        set({ flowsError: msg });
        get().addToast(msg, 'error');
        return false;
      }
    })().finally(() => {
      flowsLoadPromise = null;
      set({ flowsLoading: false });
    });
    return flowsLoadPromise;
  },

  addFlow: async (flow) => {
    try {
      await apiCreateFlow({
        flow_id: flow.id,
        yaml_content: flow.content,
        filename: flow.name,
      });
      flowMutationVersion += 1;
      if (flowsLoadPromise) await flowsLoadPromise;
      return await get().loadFlows();
    } catch (e) {
      const msg = e instanceof Error ? e.message : 'Failed to create flow';
      get().addToast(msg, 'error');
      return false;
    }
  },

  updateFlow: async (id, content) => {
    try {
      await apiUpdateFlow(id, { yaml_content: content });
      flowMutationVersion += 1;
      if (flowsLoadPromise) await flowsLoadPromise;
      return await get().loadFlows();
    } catch (e) {
      const msg = e instanceof Error ? e.message : 'Failed to update flow';
      get().addToast(msg, 'error');
      return false;
    }
  },

  deleteFlow: async (id) => {
    try {
      await apiDeleteFlow(id);
      flowMutationVersion += 1;
      if (flowsLoadPromise) await flowsLoadPromise;
      return await get().loadFlows();
    } catch (e) {
      const msg = e instanceof Error ? e.message : 'Failed to delete flow';
      get().addToast(msg, 'error');
      return false;
    }
  },

  // ============================================================
  // Dashboard
  // ============================================================

  dashboard: defaultDashboard,

  loadDashboard: async () => {
    set({ isLoading: true, apiError: null });
    try {
      const data = await fetchDashboard();
      set({ dashboard: transformDashboard(data), isLoading: false });
    } catch (e) {
      const msg = e instanceof Error ? e.message : 'Failed to load dashboard';
      set({ apiError: msg, isLoading: false });
      get().addToast(msg, 'error');
    }
  },

  setKernelStatus: (status) =>
    set((state) => ({
      dashboard: { ...state.dashboard, kernelStatus: status },
    })),

  restartKernel: async () => {
    try {
      await apiRestartKernel();
      set((state) => ({
        dashboard: { ...state.dashboard, kernelStatus: 'stopped' },
      }));
      setTimeout(() => {
        get().loadDashboard();
      }, 3000);
    } catch (e) {
      const msg = e instanceof Error ? e.message : 'Failed to restart kernel';
      get().addToast(msg, 'error');
    }
  },

  // ============================================================
  // Profile
  // ============================================================

  profile: defaultProfile,

  loadProfile: async () => {
    try {
      const data = await fetchProfile();
      set({ profile: transformProfile(data.profile) });
    } catch (e) {
      // Profile not found (404) is expected for new users
      const msg = e instanceof Error ? e.message : '';
      if (!msg.includes('Profile not found')) {
        set({ apiError: msg });
      }
    }
  },

  updateProfile: async (profileUpdate) => {
    try {
      const current = get().profile;
      const payload: Record<string, unknown> = {
        username: profileUpdate.username ?? current.username,
        language: profileUpdate.language ?? current.language,
        icon: profileUpdate.avatar ?? current.avatar,
        occupation: profileUpdate.job ?? current.job,
      };
      await apiUpdateProfile(payload);
      await get().loadProfile();
      return true;
    } catch (e) {
      const msg = e instanceof Error ? e.message : 'Failed to update profile';
      get().addToast(msg, 'error');
      return false;
    }
  },

  connectAccount: async () => {
    try {
      const data = await startOAuth();
      await openExternalUrl(data.authorize_url);
    } catch (e) {
      throw e;
    }
  },

  // ============================================================
  // Version
  // ============================================================

  version: defaultVersion,

  loadVersion: async () => {
    try {
      const data = await fetchVersion();
      set({ version: transformVersion(data) });
    } catch (e) {
      // Version fetch failure is non-critical
      const msg = e instanceof Error ? e.message : 'Failed to load version';
      console.warn('Version fetch failed:', msg);
    }
  },

  updates: [],
  autoUpdate: { tobkiri: false, defaultspack: false },
  updatesLoading: false,
  updateSettingsLoading: false,
  updateApplyingTarget: null,

  loadUpdates: async () => {
    set({ updatesLoading: true });
    try {
      const data = await fetchUpdates();
      set({ updates: data.updates.map(transformUpdateInfo), updatesLoading: false });
    } catch (e) {
      const msg = e instanceof Error ? e.message : 'Failed to check updates';
      set({ updatesLoading: false });
      get().addToast(msg, 'error');
    }
  },

  loadUpdateSettings: async () => {
    set({ updateSettingsLoading: true });
    try {
      const data = await fetchUpdateSettings();
      set({ autoUpdate: data.auto_update, updateSettingsLoading: false });
    } catch (e) {
      const msg = e instanceof Error ? e.message : 'Failed to load update settings';
      set({ updateSettingsLoading: false });
      get().addToast(msg, 'error');
    }
  },

  setAutoUpdate: async (target, enabled) => {
    const previous = get().autoUpdate;
    set({ autoUpdate: { ...previous, [target]: enabled }, updateSettingsLoading: true });
    try {
      const data = await updateUpdateSettings({ [target]: enabled });
      set({ autoUpdate: data.auto_update, updateSettingsLoading: false });
      get().addToast(`Auto update ${enabled ? 'enabled' : 'disabled'}: ${target}`, 'success');
    } catch (e) {
      const msg = e instanceof Error ? e.message : 'Failed to save update settings';
      set({ autoUpdate: previous, updateSettingsLoading: false });
      get().addToast(msg, 'error');
    }
  },

  applyUpdate: async (target) => {
    set({ updateApplyingTarget: target });
    try {
      const result = await apiApplyUpdate(target);
      await get().loadUpdates();
      const suffix = result.restart_required
        ? ' Restart Tobkiri to finish.'
        : result.routes_reload_recommended
          ? ' Restart the Kernel to reload routes.'
          : '';
      get().addToast(`Update applied: ${target}.${suffix}`, 'success');
    } catch (e) {
      const msg = e instanceof Error ? e.message : 'Failed to apply update';
      get().addToast(msg, 'error');
    } finally {
      set({ updateApplyingTarget: null });
    }
  },
}));
