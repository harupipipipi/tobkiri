import {create} from 'zustand';
import {
  approvePack as apiApprovePack,
  checkHealth,
  fetchPackVMDoctor as apiFetchPackVMDoctor,
  disablePack as apiDisablePack,
  enablePack as apiEnablePack,
  fetchFrontendCatalog,
  fetchPacks,
  installPack as apiInstallPack,
  invokeFrontendCapability,
  revokePackApproval as apiRevokePackApproval,
  parseHealthResponse,
} from './lib/api';
import type {
  ApiDynamicFrontendCatalog,
  ApiPackVMDoctor,
  PackControlBinding,
  ApiSupervisorDashboard,
  HealthResponseData,
  RuntimeStatus,
} from './lib/apiTypes';
import {transformPacks} from './lib/transforms';
import {
  COLOR_MODE_STORAGE_KEY,
  THEME_STORAGE_KEY,
  normalizeColorMode,
  normalizeTheme,
} from './lib/appearance';
import type {ColorMode, Theme} from './lib/appearance';
import {AVATAR_OPTIONS, DEFAULT_AVATAR} from './lib/avatar';
import {refreshMountedRuntimeSurfaces} from './lib/runtimeSurfaceRefresh';
import {PINNED_FRONTEND_CONTRACT_MAP_ARTIFACT_DIGEST} from './lib/generatedFrontendContractMap';
import {formatPackVMRecoveryError} from './lib/packvmLifecycle';
import {
  beginMutation,
  completeMutation,
  isMutationResultUnknown,
  listMutationJournal,
  markMutationUnknown,
  mutationRequestId,
  MUTATION_UNKNOWN_MESSAGE,
  MutationBlockedError,
  MutationResultUnknownError,
  type MutationJournalRecord,
} from './lib/mutationJournal';
import {
  reconcileMutationStatus,
  type OperationStatus,
  type OperationStatusState,
} from './lib/operationStatus';
import {recordClientDiagnostic} from './lib/clientDiagnostics';
import {setRuntimeDispatchStatus} from './lib/runtimeDispatchGate';
import {
  getBrowserStorage,
  readSafeStorageValue,
  writeSafeStorageValue,
} from './lib/safeStorage';
import {
  DEVTOOLS_PREFERENCE_STORAGE_KEY,
  normalizeDevtoolsEnabled,
} from './lib/devtoolsPreference';

export type {ColorMode, Theme} from './lib/appearance';
export {AVATAR_OPTIONS} from './lib/avatar';

function readLocalStorage(key: string): string | null {
  return readSafeStorageValue(getBrowserStorage('local'), key);
}

function writeLocalStorage(key: string, value: string): void {
  writeSafeStorageValue(getBrowserStorage('local'), key, value);
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

export interface PackOperation {
  operationId: string;
  contractId: string;
  providerId: string;
  capabilities: string[];
  inputSchema: Record<string, unknown>;
  invokable: boolean;
}

export interface Pack {
  id: string;
  name: string;
  version: string;
  type: 'core' | 'community';
  required?: boolean;
  installed: boolean;
  enabled: boolean;
  description: string;
  artifactDigest: string;
  profileId: string;
  workspaceId: string;
  profileRevision: string;
  planDigest: string;
  catalogRevision: string;
  approvalStatus: string;
  approvalReason: string | null;
  approved: boolean;
  hashValid: boolean | null;
  criticalChanged: boolean | null;
  approvalIssues: string[];
  capabilities: {name: string; description: string}[];
  operations?: PackOperation[];
  flows: string[];
  dependencies: string[];
}

export interface PackVMDoctorRefreshOptions {
  /** Skip projection loads when the caller owns the authoritative sequence. */
  reconcile?: boolean;
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

export type {RuntimeStatus} from './lib/apiTypes';

const SIDEBAR_STORAGE_KEY = 'tobkiri-launcher-sidebar-open';
const LEGACY_SIDEBAR_STORAGE_KEY = 'rumi-viewer-sidebar-open';
const SETUP_STORAGE_KEY = 'tobkiri-launcher-setup';
const LEGACY_SETUP_STORAGE_KEY = 'rumi-setup';
const PROFILE_STORAGE_KEY = 'tobkiri-launcher-local-profile';

interface AppState {
  theme: Theme;
  setTheme: (theme: Theme) => void;
  colorMode: ColorMode;
  setColorMode: (mode: ColorMode) => void;
  isSetupDone: boolean;
  setSetupDone: (done: boolean) => void;
  isSidebarOpen: boolean;
  setSidebarOpen: (open: boolean) => void;
  devtoolsEnabled: boolean;
  setDevtoolsEnabled: (enabled: boolean) => void;
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
  hostCatalogVerified: boolean;
  profileCeremonyAvailable: boolean;
  defaultsBootstrapRequired: boolean;
  activeProfileReady: boolean;
  launchReady: boolean;
  lastRuntimeHealthyAt: number | null;
  setRuntimeHealth: (health: HealthResponseData) => void;
  refreshRuntimeHealth: () => Promise<void>;
  packs: Pack[];
  packCatalogBinding: PackControlBinding | null;
  packsLoading: boolean;
  packsError: string | null;
  packInstallPending: Record<string, boolean>;
  packTogglePending: Record<string, boolean>;
  packApprovalPending: Record<string, boolean>;
  frontendCatalog: ApiDynamicFrontendCatalog | null;
  frontendCatalogLoading: boolean;
  frontendCatalogError: string | null;
  packOperationPending: Record<string, boolean>;
  packMutationUnknown: Record<string, MutationJournalRecord>;
  packOperationUnknown: Record<string, MutationJournalRecord>;
  packVmDoctor: ApiPackVMDoctor | null;
  packVmDoctorLoading: boolean;
  packVmError: string | null;
  loadPacks: (
    force?: boolean,
    options?: {skipMutationReconciliation?: boolean},
  ) => Promise<void>;
  loadFrontendCatalog: (force?: boolean) => Promise<void>;
  refreshPackVMDoctor: (
    options?: PackVMDoctorRefreshOptions,
  ) => Promise<ApiPackVMDoctor | null>;
  setPackVMDoctor: (doctor: ApiPackVMDoctor | null) => void;
  invokePackOperation: (
    packId: string,
    operationId: string,
    payload: Record<string, unknown>,
  ) => Promise<unknown>;
  installPack: (id: string) => Promise<void>;
  approvePack: (id: string) => Promise<void>;
  revokePackApproval: (id: string) => Promise<void>;
  togglePack: (id: string) => Promise<boolean>;
  profile: Profile;
  updateLocalProfile: (profile: Partial<Pick<Profile, 'avatar' | 'username' | 'language' | 'job'>>) => void;
}

const defaultProfile: Profile = {
  avatar: DEFAULT_AVATAR,
  username: 'User',
  language: 'en',
  job: '',
  connected: false,
};

function readLocalProfile(): Profile {
  const raw = readLocalStorage(PROFILE_STORAGE_KEY);
  if (!raw) return defaultProfile;
  try {
    const value = JSON.parse(raw) as Record<string, unknown>;
    const username = typeof value.username === 'string' && value.username.trim()
      ? value.username.trim().slice(0, 80)
      : defaultProfile.username;
    const avatar = typeof value.avatar === 'string' && AVATAR_OPTIONS.includes(value.avatar)
      ? value.avatar
      : defaultProfile.avatar;
    const language = typeof value.language === 'string' && ['en', 'ja'].includes(value.language)
      ? value.language
      : defaultProfile.language;
    const job = typeof value.job === 'string' ? value.job.slice(0, 120) : defaultProfile.job;
    return {...defaultProfile, avatar, username, language, job};
  } catch {
    return defaultProfile;
  }
}

let packsLoadPromise: Promise<void> | null = null;
let frontendCatalogLoadPromise: Promise<void> | null = null;
type PackVMDoctorRefreshMode = 'observe' | 'reconcile';

// Observe-only Setup recovery must never inherit reconcile-mode projection
// work. Keep the modes separate while the generation rejects stale updates.
const packVmDoctorLoadPromises = new Map<
  PackVMDoctorRefreshMode,
  Promise<ApiPackVMDoctor | null>
>();
let packVmDoctorRefreshGeneration = 0;
const packMutationVersions = new Map<string, number>();
let packMutationEpoch = 0;
let packInvalidationRequested = 0;
let packInvalidationPromise: Promise<void> | null = null;

function journalRecordsForKind(kind: string | null): Record<string, MutationJournalRecord> {
  return Object.fromEntries(
    listMutationJournal()
      .filter((record) => record.state !== 'pending'
        && (kind === null
          ? typeof record.metadata.kind === 'string'
            && record.metadata.kind.startsWith('pack.')
            && record.metadata.kind !== 'pack.operation'
          : record.metadata.kind === kind))
      .map((record) => [record.key, record]),
  );
}

function packRecordMetadata(record: MutationJournalRecord): Record<string, unknown> {
  return record.metadata;
}

function matchingPackMutation(record: MutationJournalRecord, pack: Pack): boolean {
  const metadata = packRecordMetadata(record);
  if (metadata.pack_id !== pack.id) return false;
  switch (metadata.kind) {
    case 'pack.install':
      return metadata.expected_installed === true && pack.installed === true;
    case 'pack.approve':
      return metadata.expected_approved === true
        && pack.approved === true
        && pack.approvalStatus === 'approved';
    case 'pack.revoke':
      return metadata.expected_approved === false
        && metadata.expected_enabled === false
        && pack.approved === false
        && pack.enabled === false
        && pack.approvalStatus === 'revoked';
    case 'pack.toggle':
      return typeof metadata.expected_enabled === 'boolean'
        && pack.enabled === metadata.expected_enabled;
    default:
      return false;
  }
}

function reconcilePackMutationJournal(
  packs: Pack[],
  records: Record<string, MutationJournalRecord>,
): Record<string, MutationJournalRecord> {
  const remaining = {...records};
  for (const record of Object.values(records)) {
    // A projection match is evidence for the UI, not proof that this exact
    // request was applied. Only the authenticated operation-status endpoint
    // may release an unknown journal entry.
    if (record.metadata.status_reconciled !== true) continue;
    const packId = record.metadata.pack_id;
    const pack = typeof packId === 'string' ? packs.find((item) => item.id === packId) : undefined;
    if (!pack || !matchingPackMutation(record, pack)) continue;
    completeMutation(record.key, record.requestId);
    delete remaining[record.key];
  }
  return remaining;
}

function beginPackMutation(id: string): number {
  const version = (packMutationVersions.get(id) ?? 0) + 1;
  packMutationVersions.set(id, version);
  packMutationEpoch += 1;
  return version;
}

async function invalidatePackMutationSurfaces(get: () => AppState): Promise<void> {
  packInvalidationRequested += 1;
  if (packInvalidationPromise) return packInvalidationPromise;

  packInvalidationPromise = (async () => {
    let handled = 0;
    while (handled < packInvalidationRequested) {
      const requested = packInvalidationRequested;
      const packVmExplicitlyUnavailable = get().packVmDoctor?.ready === false;
      const refreshes: Promise<void>[] = [
        // Operation-status reconciliation owns the current refresh. Do not
        // start a second hydrated-journal reconciliation from that refresh;
        // it would outlive the caller and could issue a later request against
        // a replaced/closed UI context.
        get().loadPacks(true, {skipMutationReconciliation: true}),
      ];
      // The dynamic capability catalog is a PackVM-owned surface. An
      // intentionally unavailable PackVM (for example an ad-hoc signed macOS
      // build) must not turn a successful Host-owned Pack lifecycle mutation
      // into an indeterminate result.
      if (!packVmExplicitlyUnavailable) refreshes.push(get().loadFrontendCatalog(true));
      await Promise.all(refreshes);
      // Readiness may become authoritative while the Host catalog refresh is
      // in flight. In that case the PackVM-owned projection is required after
      // all and must be refreshed before the mutation can be confirmed.
      if (packVmExplicitlyUnavailable && get().packVmDoctor?.ready === true) {
        await get().loadFrontendCatalog(true);
      }
      const refreshedState = get();
      if (
        refreshedState.packsError
        || (
          refreshedState.packVmDoctor?.ready !== false
          && refreshedState.frontendCatalogError
        )
      ) {
        throw new Error('Authoritative Pack projections could not be reconciled.');
      }
      await refreshMountedRuntimeSurfaces();
      handled = requested;
    }
  })().finally(() => {
    packInvalidationPromise = null;
  });
  return packInvalidationPromise;
}

const PACK_CONTROL_CONTRACT = 'tobkiri.host.pack-control.v4';
interface HydratedPackStatusTask {
  controller: AbortController;
  promise: Promise<void>;
}

const hydratedPackStatusRequests = new Map<string, HydratedPackStatusTask>();

export interface PackMutationReconciliationHandle {
  readonly promise: Promise<void>;
  readonly cancel: () => void;
}

/** Wait for all store-owned restart reconciliation work to reach quiescence. */
export async function waitForPackMutationReconciliation(): Promise<void> {
  while (hydratedPackStatusRequests.size > 0) {
    const tasks = [...hydratedPackStatusRequests.values()];
    await Promise.all(tasks.map((task) => task.promise));
  }
}

/** Return the coalesced lifecycle handle used by a page or session owner. */
export function getPackMutationReconciliationHandle(): PackMutationReconciliationHandle {
  return {
    promise: waitForPackMutationReconciliation(),
    cancel: cancelPackMutationReconciliation,
  };
}

/** Cancel queued restart reconciliation without releasing its durable journal. */
export function cancelPackMutationReconciliation(): void {
  for (const task of hydratedPackStatusRequests.values()) {
    task.controller.abort(new Error('Pack mutation reconciliation was cancelled.'));
  }
}

async function reconcilePackMutationStatus(
  record: MutationJournalRecord,
  get: () => AppState,
  operationId: string,
  verifySuccess?: (status: OperationStatus) => boolean,
  options: {
    requestId?: string;
    statusPhase?: string;
    completeOnTerminal?: boolean;
    contractId?: string;
    signal?: AbortSignal;
  } = {},
): Promise<{state: OperationStatusState | 'stale'; status: OperationStatus; reconciled: boolean}> {
  const expectedJournalOperation = options.statusPhase === 'approval'
    ? 'approval.candidate'
    : operationId;
  if (
    record.metadata.operation_id !== expectedJournalOperation
    || record.metadata.contract_id !== (options.contractId ?? PACK_CONTROL_CONTRACT)
    || record.metadata.contract_map_digest !== PINNED_FRONTEND_CONTRACT_MAP_ARTIFACT_DIGEST
  ) {
    throw new Error('The journaled Pack operation binding changed.');
  }
  return reconcileMutationStatus({
    record,
    binding: {
      requestId: options.requestId ?? record.requestId,
      operationId,
      contractId: options.contractId ?? PACK_CONTROL_CONTRACT,
      mapArtifactDigest: PINNED_FRONTEND_CONTRACT_MAP_ARTIFACT_DIGEST,
    },
    statusPhase: options.statusPhase,
    refresh: () => invalidatePackMutationSurfaces(get),
    verifySuccess,
    completeOnTerminal: options.completeOnTerminal,
    signal: options.signal,
  });
}

function packMutationSuccess(record: MutationJournalRecord, get: () => AppState): boolean {
  const packId = record.metadata.pack_id;
  const pack = typeof packId === 'string'
    ? get().packs.find((candidate) => candidate.id === packId)
    : undefined;
  return Boolean(pack && matchingPackMutation(record, pack));
}

function clearPackUnknownState(
  set: (update: Partial<AppState> | ((state: AppState) => Partial<AppState>)) => void,
  record: MutationJournalRecord,
): void {
  set((current) => {
    if (record.metadata.kind === 'pack.operation') {
      const next = {...current.packOperationUnknown};
      delete next[record.key];
      return {packOperationUnknown: next};
    }
    const next = {...current.packMutationUnknown};
    delete next[record.key];
    return {packMutationUnknown: next};
  });
}

function scheduleHydratedPackStatusReconciliation(
  get: () => AppState,
  set: (update: Partial<AppState> | ((state: AppState) => Partial<AppState>)) => void,
): void {
  const records = listMutationJournal().filter((record) => (
    record.state !== 'pending'
    && typeof record.metadata.kind === 'string'
    && record.metadata.kind.startsWith('pack.')
  ));
  for (const record of records) {
    if (hydratedPackStatusRequests.has(record.key)) continue;
    const controller = new AbortController();
    const task: HydratedPackStatusTask = {
      controller,
      promise: Promise.resolve(),
    };
    task.promise = (async () => {
      try {
        let reconciled: Awaited<ReturnType<typeof reconcilePackMutationStatus>>;
        if (record.metadata.kind === 'pack.operation') {
          const operationId = record.metadata.operation_id;
          const contractId = record.metadata.contract_id;
          if (typeof operationId !== 'string' || typeof contractId !== 'string') return;
          reconciled = await reconcilePackMutationStatus(
            record,
            get,
            operationId,
            () => true,
            {contractId, signal: controller.signal},
          );
        } else if (record.metadata.kind === 'pack.approve') {
          const approval = await reconcilePackApprovalStatus(record, get, controller.signal);
          if (approval.state === 'succeeded' || approval.state === 'failed') {
            clearPackUnknownState(set, record);
          }
          return;
        } else {
          const operationId = record.metadata.operation_id;
          if (typeof operationId !== 'string') return;
          reconciled = await reconcilePackMutationStatus(
            record,
            get,
            operationId,
            (status) => status.state === 'succeeded' && packMutationSuccess(record, get),
            {signal: controller.signal},
          );
        }
        if (reconciled.state === 'succeeded' || reconciled.state === 'failed') {
          clearPackUnknownState(set, record);
        }
      } catch (error) {
        recordClientDiagnostic({
          code: 'pack.mutation.reconciliation_failed',
          operation: 'hydrate.pack.mutation',
          error,
        });
      }
    })().finally(() => {
      if (hydratedPackStatusRequests.get(record.key) === task) {
        hydratedPackStatusRequests.delete(record.key);
      }
    });
    hydratedPackStatusRequests.set(record.key, task);
  }
}

async function reconcilePackApprovalStatus(
  record: MutationJournalRecord,
  get: () => AppState,
  signal?: AbortSignal,
): Promise<{state: OperationStatusState | 'stale'; status: OperationStatus; reconciled: boolean}> {
  const candidate = await reconcilePackMutationStatus(
    record,
    get,
    'approval.candidate',
    undefined,
    {
      requestId: mutationRequestId(record, 'candidate'),
      statusPhase: 'candidate',
      completeOnTerminal: false,
      signal,
    },
  );
  if (candidate.state !== 'succeeded') {
    if (candidate.state === 'failed') completeMutation(record.key, record.requestId);
    return candidate;
  }

  const approval = await reconcilePackMutationStatus(
    record,
    get,
    'approval.approve',
    (status) => status.state === 'succeeded' && packMutationSuccess(record, get),
    {
      requestId: mutationRequestId(record, 'approval'),
      statusPhase: 'approval',
      completeOnTerminal: false,
      signal,
    },
  );
  if (
    approval.state !== 'pending'
    && approval.state !== 'indeterminate'
    && approval.state !== 'stale'
  ) {
    completeMutation(record.key, record.requestId);
  }
  return approval;
}

export const useAppStore = create<AppState>((set, get) => ({
  theme: normalizeTheme(readLocalStorage(THEME_STORAGE_KEY)),
  setTheme: (theme) => {
    writeLocalStorage(THEME_STORAGE_KEY, theme);
    set({theme});
  },

  colorMode: normalizeColorMode(readLocalStorage(COLOR_MODE_STORAGE_KEY)),
  setColorMode: (mode) => {
    writeLocalStorage(COLOR_MODE_STORAGE_KEY, mode);
    set({colorMode: mode});
  },

  isSetupDone:
    (readLocalStorage(SETUP_STORAGE_KEY) ?? readLocalStorage(LEGACY_SETUP_STORAGE_KEY)) === 'true',
  setSetupDone: (done) => {
    if (!done) cancelPackMutationReconciliation();
    writeLocalStorage(SETUP_STORAGE_KEY, String(done));
    set({isSetupDone: done});
  },

  isSidebarOpen:
    (readLocalStorage(SIDEBAR_STORAGE_KEY) ?? readLocalStorage(LEGACY_SIDEBAR_STORAGE_KEY)) !== 'false',
  setSidebarOpen: (open) => {
    writeLocalStorage(SIDEBAR_STORAGE_KEY, String(open));
    set({isSidebarOpen: open});
  },

  devtoolsEnabled: normalizeDevtoolsEnabled(
    readLocalStorage(DEVTOOLS_PREFERENCE_STORAGE_KEY),
  ),
  setDevtoolsEnabled: (enabled) => {
    writeLocalStorage(DEVTOOLS_PREFERENCE_STORAGE_KEY, String(enabled));
    set({devtoolsEnabled: enabled});
  },

  toasts: [],
  addToast: (message, type) => {
    const id = Math.random().toString(36).substring(2, 9);
    set((state) => ({toasts: [...state.toasts, {id, message, type}]}));
    setTimeout(() => {
      set((state) => ({toasts: state.toasts.filter((toast) => toast.id !== id)}));
    }, 3000);
  },
  removeToast: (id) => set((state) => ({toasts: state.toasts.filter((toast) => toast.id !== id)})),

  dialog: null,
  showDialog: (config) => set({dialog: config}),
  closeDialog: () => set({dialog: null}),

  isLoading: false,
  apiError: null,

  runtimeReady: false,
  runtimeStatus: 'starting',
  runtimeError: null,
  runtimeDisconnected: false,
  hostCatalogVerified: false,
  profileCeremonyAvailable: false,
  defaultsBootstrapRequired: false,
  activeProfileReady: false,
  launchReady: false,
  lastRuntimeHealthyAt: null,
  setRuntimeHealth: (health) => {
    let parsedHealth: HealthResponseData;
    try {
      parsedHealth = parseHealthResponse(health);
    } catch (error) {
      recordClientDiagnostic({
        code: 'runtime.health_contract_invalid',
        operation: 'runtime.health',
        error,
      });
      setRuntimeDispatchStatus('error');
      set((state) => ({
        runtimeReady: false,
        runtimeStatus: 'error',
        runtimeError: 'Runtime health response failed validation.',
        runtimeDisconnected: state.lastRuntimeHealthyAt !== null,
      }));
      throw error;
    }
    setRuntimeDispatchStatus(parsedHealth.runtime_status);
    set((state) => ({
      runtimeReady: parsedHealth.runtime_ready,
      runtimeStatus: parsedHealth.runtime_status,
      runtimeError: parsedHealth.runtime_error,
      runtimeDisconnected: false,
      hostCatalogVerified: parsedHealth.host_catalog_verified,
      profileCeremonyAvailable: parsedHealth.profile_ceremony_available,
      defaultsBootstrapRequired: parsedHealth.defaults_bootstrap_required,
      activeProfileReady: parsedHealth.active_profile_ready,
      launchReady: parsedHealth.launch_ready,
      lastRuntimeHealthyAt: parsedHealth.runtime_ready ? Date.now() : state.lastRuntimeHealthyAt,
    }));
  },
  refreshRuntimeHealth: async () => {
    try {
      const health = await checkHealth();
      get().setRuntimeHealth(health);
    } catch (error) {
      const message = error instanceof Error ? error.message : 'Failed to read runtime health';
      setRuntimeDispatchStatus('error');
      set((state) => ({
        runtimeReady: false,
        runtimeStatus: 'error',
        runtimeError: message,
        runtimeDisconnected: state.lastRuntimeHealthyAt !== null,
      }));
    }
  },

  packs: [],
  packCatalogBinding: null,
  packsLoading: false,
  packsError: null,
  packInstallPending: {},
  packTogglePending: {},
  packApprovalPending: {},
  frontendCatalog: null,
  frontendCatalogLoading: false,
  frontendCatalogError: null,
  packOperationPending: {},
  packMutationUnknown: journalRecordsForKind(null),
  packOperationUnknown: journalRecordsForKind('pack.operation'),
  packVmDoctor: null,
  packVmDoctorLoading: false,
  packVmError: null,

  loadPacks: (force = false, options = {}) => {
    if (packsLoadPromise) {
      if (!force) return packsLoadPromise;
      const inFlight = packsLoadPromise;
      return inFlight.then(() => get().loadPacks(true, options));
    }
    const versionsAtStart = new Map(packMutationVersions);
    set({packsLoading: true, packsError: null});
    packsLoadPromise = (async () => {
      try {
        const data = await fetchPacks();
        const latestState = get();
        const currentById = new Map(latestState.packs.map((pack) => [pack.id, pack]));
        const packs = transformPacks(data.packs).map((pack) => {
          const before = versionsAtStart.get(pack.id) ?? 0;
          const after = packMutationVersions.get(pack.id) ?? 0;
          if (
            before === after
            && !latestState.packTogglePending[pack.id]
            && !latestState.packApprovalPending[pack.id]
            && !latestState.packInstallPending[pack.id]
          ) return pack;
          const current = currentById.get(pack.id);
          if (!current) return pack;
          let reconciled = pack;
          if (latestState.packInstallPending[pack.id]) {
            reconciled = {...reconciled, installed: current.installed};
          }
          if (
            latestState.packApprovalPending[pack.id]
            || (current.approvalStatus === 'revoked' && !current.approved)
          ) {
            return {
              ...reconciled,
              enabled: current.enabled,
              approved: current.approved,
              approvalStatus: current.approvalStatus,
              approvalReason: current.approvalReason,
              approvalIssues: current.approvalIssues,
            };
          }
          if (latestState.packTogglePending[pack.id]) {
            return {...reconciled, enabled: current.enabled};
          }
          return reconciled;
        });
        const durablePackUnknown = {
          ...journalRecordsForKind(null),
          ...get().packMutationUnknown,
        };
        const reconciledPackUnknown = reconcilePackMutationJournal(packs, durablePackUnknown);
        set({
          packs,
          packCatalogBinding: {
            profile_id: data.profile_id,
            workspace_id: data.workspace_id,
            profile_revision: data.profile_revision,
            plan_digest: data.plan_digest,
            catalog_revision: data.catalog_revision,
          },
          packsError: null,
          packMutationUnknown: reconciledPackUnknown,
        });
        if (!options.skipMutationReconciliation) {
          scheduleHydratedPackStatusReconciliation(get, set);
        }
      } catch (error) {
        const message = error instanceof Error ? error.message : 'Failed to load packs';
        set({packsError: message});
        get().addToast(message, 'error');
      }
    })().finally(() => {
      packsLoadPromise = null;
      set({packsLoading: false});
    });
    return packsLoadPromise;
  },

  loadFrontendCatalog: (force = false) => {
    if (!get().packVmDoctor?.ready) {
      set({
        frontendCatalog: null,
        frontendCatalogError: 'PackVM catalog access is blocked until healthy attestation.',
      });
      return Promise.resolve();
    }
    if (frontendCatalogLoadPromise) {
      if (!force) return frontendCatalogLoadPromise;
      const inFlight = frontendCatalogLoadPromise;
      return inFlight.then(() => get().loadFrontendCatalog(true));
    }
    const mutationEpochAtStart = packMutationEpoch;
    set({
      frontendCatalogLoading: true,
      frontendCatalogError: null,
      frontendCatalog: null,
    });
    frontendCatalogLoadPromise = (async () => {
      try {
        const catalog = await fetchFrontendCatalog();
        if (mutationEpochAtStart !== packMutationEpoch) return;
        if (!get().packVmDoctor?.ready) {
          set({
            frontendCatalog: null,
            frontendCatalogError: 'PackVM catalog access is blocked until healthy attestation.',
          });
          return;
        }
        if (
          !catalog.profile_id
          || !catalog.profile_revision
          || !catalog.activation_id
          || !catalog.plan_hash
          || !catalog.catalog_hash
          || !Array.isArray(catalog.contributions)
        ) {
          throw new Error('Tobkiri returned an invalid dynamic frontend catalog.');
        }
        set({frontendCatalog: catalog, frontendCatalogError: null});
      } catch (error) {
        const message = error instanceof Error
          ? error.message
          : 'Tobkiri dynamic frontend catalog is unavailable.';
        set({frontendCatalog: null, frontendCatalogError: message});
      }
    })().finally(() => {
      frontendCatalogLoadPromise = null;
      set({frontendCatalogLoading: false});
    });
    return frontendCatalogLoadPromise;
  },

  setPackVMDoctor: (doctor) => {
    set({
      packVmDoctor: doctor,
      ...(doctor?.ready
        ? {packVmError: null}
        : {
          frontendCatalog: null,
          frontendCatalogError: doctor?.reason
            || 'PackVM catalog access is blocked until healthy attestation.',
        }),
    });
  },

  refreshPackVMDoctor: (options = {}) => {
    const mode: PackVMDoctorRefreshMode = options.reconcile === false
      ? 'observe'
      : 'reconcile';
    const existingFlight = packVmDoctorLoadPromises.get(mode);
    if (existingFlight) return existingFlight;
    const generation = ++packVmDoctorRefreshGeneration;
    set({packVmDoctorLoading: true, packVmError: null});

    let flight!: Promise<ApiPackVMDoctor | null>;
    flight = (async () => {
      try {
        const doctor = await apiFetchPackVMDoctor();
        const isCurrent = generation === packVmDoctorRefreshGeneration;
        if (isCurrent) {
          set({
            packVmDoctor: doctor,
            packVmError: doctor.ready
              ? null
              : (doctor.reason || 'PackVM is not ready for Pack operations.'),
          });
        }
        if (doctor.ready && mode === 'reconcile' && isCurrent) {
          await Promise.all([get().loadPacks(), get().loadFrontendCatalog()]);
        } else if (!doctor.ready && isCurrent) {
          set({
            frontendCatalog: null,
            frontendCatalogError: doctor.reason || 'PackVM is not attested and ready.',
          });
        }
        return doctor;
      } catch (error) {
        const message = formatPackVMRecoveryError(
          error,
          'PackVM readiness could not be verified.',
        );
        if (generation === packVmDoctorRefreshGeneration) {
          set({
            packVmDoctor: null,
            packVmError: message,
            frontendCatalog: null,
            frontendCatalogError: message,
          });
        }
        return null;
      }
    })().finally(() => {
      if (packVmDoctorLoadPromises.get(mode) === flight) {
        packVmDoctorLoadPromises.delete(mode);
      }
      set({packVmDoctorLoading: packVmDoctorLoadPromises.size > 0});
    });
    packVmDoctorLoadPromises.set(mode, flight);
    return flight;
  },

  invokePackOperation: async (packId, operationId, payload) => {
    const operationKey = `${packId}:${operationId}`;
    const state = get();
    if (!state.packVmDoctor?.ready) {
      throw new Error(
        'Tobkiri keeps Pack operations unavailable until PackVM health is attested.',
      );
    }
    const pack = state.packs.find((candidate) => candidate.id === packId);
    if (!pack || !pack.installed || !pack.enabled || !pack.approved) {
      throw new Error(
        'Tobkiri requires an installed, approved, enabled Pack before invoking its operation.',
      );
    }
    if (state.packOperationPending[operationKey]) {
      throw new Error('Tobkiri operation is already in progress.');
    }
    const durableOperationUnknown = {
      ...journalRecordsForKind('pack.operation'),
      ...state.packOperationUnknown,
    };
    const unknownOperation = Object.values(durableOperationUnknown).find((record) => (
      record.metadata.pack_id === packId && record.metadata.operation_id === operationId
    ));
    if (unknownOperation) throw new MutationBlockedError(unknownOperation);
    const operation = (pack.operations ?? []).find(
      (candidate) => candidate.operationId === operationId,
    );
    const catalog = state.frontendCatalog;
    const contribution = catalog?.contributions.find((candidate) => (
      candidate.owner_pack_id === packId
      && candidate.action_contract
      && (
        candidate.operation_id === operationId
        || candidate.contribution_id === operationId
        || candidate.label === operationId
      )
    ));
    if (!operation || !catalog || catalog.quarantined_pack_ids.includes(packId) || !contribution) {
      throw new Error(
        'Tobkiri has not exposed this Pack operation in the current v4 capability catalog.',
      );
    }
    if (
      !operation.invokable
      || contribution.action_contract !== operation.contractId
      || contribution.resolved_profile_id !== catalog.profile_id
      || contribution.resolved_profile_revision !== catalog.profile_revision
      || contribution.resolved_activation_id !== catalog.activation_id
      || contribution.resolved_plan_hash !== catalog.plan_hash
    ) {
      throw new Error('Tobkiri has not verified this Pack operation for invocation.');
    }

    const mutationKey = `pack:operation:${packId}:${operationId}:${JSON.stringify(payload)}`;
    const mutation = beginMutation(mutationKey, {
      kind: 'pack.operation',
      pack_id: packId,
      operation_id: operationId,
      contract_id: contribution.action_contract,
      contract_map_digest: PINNED_FRONTEND_CONTRACT_MAP_ARTIFACT_DIGEST,
    });
    set((current) => ({
      packOperationPending: {...current.packOperationPending, [operationKey]: true},
    }));
    let responseAccepted = false;
    try {
      const result = await invokeFrontendCapability({
        profileId: catalog.profile_id,
        profileRevision: catalog.profile_revision,
        activationId: catalog.activation_id,
        planHash: catalog.plan_hash,
        catalogHash: catalog.catalog_hash,
        contributionId: contribution.contribution_id,
        ownerPackId: contribution.owner_pack_id,
        contractId: contribution.action_contract,
        payload,
      }, {requestId: mutation.requestId});
      responseAccepted = true;
      await invalidatePackMutationSurfaces(get);
      completeMutation(mutationKey, mutation.requestId);
      return result;
    } catch (error) {
      if (responseAccepted || isMutationResultUnknown(error)) {
        const unknown = markMutationUnknown(mutationKey, mutation.requestId);
        set((current) => ({
          packOperationUnknown: {...current.packOperationUnknown, [mutationKey]: unknown},
        }));
        let reconciled: Awaited<ReturnType<typeof reconcilePackMutationStatus>> | null = null;
        try {
          reconciled = await reconcilePackMutationStatus(
            unknown,
            get,
            operationId,
            () => true,
            {contractId: contribution.action_contract},
          );
        } catch (error) {
          recordClientDiagnostic({
            code: 'pack.mutation.reconciliation_failed',
            operation: 'invoke.pack.operation',
            error,
          });
        }
        if (reconciled?.state === 'succeeded') {
          set((current) => {
            const next = {...current.packOperationUnknown};
            delete next[mutationKey];
            return {packOperationUnknown: next};
          });
          get().addToast('Pack operation reconciled by the Host.', 'success');
          return reconciled.status.result;
        }
        if (reconciled?.state === 'failed') {
          set((current) => {
            const next = {...current.packOperationUnknown};
            delete next[mutationKey];
            return {packOperationUnknown: next};
          });
          const code = reconciled.status.safe_error_code ?? 'OPERATION_FAILED';
          const failure = new Error(`Pack operation was denied or failed (${code}).`);
          get().addToast(failure.message, 'error');
          throw failure;
        }
        get().addToast(MUTATION_UNKNOWN_MESSAGE, 'error');
        throw new MutationResultUnknownError(mutationKey, mutation.requestId);
      }
      completeMutation(mutationKey, mutation.requestId);
      throw error;
    } finally {
      set((current) => {
        const pending = {...current.packOperationPending};
        delete pending[operationKey];
        return {packOperationPending: pending};
      });
    }
  },

  installPack: async (id) => {
    const state = get();
    if (state.packInstallPending[id]) return;
    const mutationKey = `pack:install:${id}`;
    const mutation = beginMutation(mutationKey, {
      kind: 'pack.install',
      pack_id: id,
      expected_installed: true,
      operation_id: 'pack.install',
      contract_id: PACK_CONTROL_CONTRACT,
      contract_map_digest: PINNED_FRONTEND_CONTRACT_MAP_ARTIFACT_DIGEST,
    });
    const version = beginPackMutation(id);
    set((current) => ({packInstallPending: {...current.packInstallPending, [id]: true}}));
    let responseAccepted = false;
    try {
      const response = await apiInstallPack(id);
      if (response.pack_id !== id || response.installed !== true) {
        throw new Error('Tobkiri did not confirm Pack installation.');
      }
      responseAccepted = true;
      if (packMutationVersions.get(id) === version) {
        set((current) => ({
          packs: current.packs.map((candidate) => (
            candidate.id === id
              ? {
                ...candidate,
                installed: true,
                profileId: response.profile_id,
                workspaceId: response.workspace_id,
                profileRevision: response.profile_revision,
                planDigest: response.plan_digest,
                catalogRevision: response.catalog_revision,
              }
              : candidate
          )),
        }));
      }
      await invalidatePackMutationSurfaces(get);
      completeMutation(mutationKey, mutation.requestId);
      get().addToast('Pack installed.', 'success');
    } catch (error) {
      if (responseAccepted || isMutationResultUnknown(error)) {
        const unknown = markMutationUnknown(mutationKey, mutation.requestId);
        set((current) => ({
          packMutationUnknown: {...current.packMutationUnknown, [mutationKey]: unknown},
        }));
        let reconciled: Awaited<ReturnType<typeof reconcilePackMutationStatus>> | null = null;
        try {
          reconciled = await reconcilePackMutationStatus(
            unknown,
            get,
            'pack.install',
            (status) => status.state === 'succeeded' && packMutationSuccess(unknown, get),
          );
        } catch (error) {
          recordClientDiagnostic({
            code: 'pack.mutation.reconciliation_failed',
            operation: 'install.pack',
            error,
          });
        }
        if (reconciled?.state === 'succeeded') {
          set((current) => {
            const next = {...current.packMutationUnknown};
            delete next[mutationKey];
            return {packMutationUnknown: next};
          });
          get().addToast('Pack installation reconciled by the Host.', 'success');
          return;
        }
        if (reconciled?.state === 'failed') {
          set((current) => {
            const next = {...current.packMutationUnknown};
            delete next[mutationKey];
            return {packMutationUnknown: next};
          });
          const code = reconciled.status.safe_error_code ?? 'PACK_INSTALL_FAILED';
          const failure = new Error(`Pack installation was denied or failed (${code}).`);
          get().addToast(failure.message, 'error');
          throw failure;
        }
        get().addToast(MUTATION_UNKNOWN_MESSAGE, 'error');
        throw new MutationResultUnknownError(mutationKey, mutation.requestId);
      }
      completeMutation(mutationKey, mutation.requestId);
      const message = error instanceof Error ? error.message : 'Failed to install pack';
      get().addToast(message, 'error');
      throw error;
    } finally {
      set((current) => {
        const pending = {...current.packInstallPending};
        delete pending[id];
        return {packInstallPending: pending};
      });
    }
  },

  approvePack: async (id) => {
    const state = get();
    if (state.packApprovalPending[id]) return;
    const mutationKey = `pack:approve:${id}`;
    const candidateRequestId = crypto.randomUUID();
    const approvalRequestId = crypto.randomUUID();
    const mutation = beginMutation(
      mutationKey,
      {
        kind: 'pack.approve',
        pack_id: id,
        expected_approved: true,
        operation_id: 'approval.candidate',
        contract_id: PACK_CONTROL_CONTRACT,
        contract_map_digest: PINNED_FRONTEND_CONTRACT_MAP_ARTIFACT_DIGEST,
      },
      {primary: candidateRequestId, candidate: candidateRequestId, approval: approvalRequestId},
    );
    const version = beginPackMutation(id);
    set((current) => ({
      packApprovalPending: {...current.packApprovalPending, [id]: true},
    }));
    let responseAccepted = false;
    try {
      const response = await apiApprovePack(id, {
        candidateRequestId: mutationRequestId(mutation, 'candidate'),
        approvalRequestId: mutationRequestId(mutation, 'approval'),
      });
      if (
        response.pack_id !== id
        || response.approved !== true
        || response.approval_status !== 'approved'
        || (response.enabled !== undefined && typeof response.enabled !== 'boolean')
      ) {
        throw new Error('Tobkiri did not confirm Pack approval.');
      }
      responseAccepted = true;
      if (packMutationVersions.get(id) === version) {
        set((current) => ({
          packs: current.packs.map((candidate) => (
            candidate.id === id
              ? {
                ...candidate,
                ...(response.enabled === undefined ? {} : {enabled: response.enabled}),
                approved: true,
                approvalStatus: 'approved',
                approvalReason: null,
                approvalIssues: [],
                profileId: response.profile_id,
                workspaceId: response.workspace_id,
                profileRevision: response.profile_revision,
                planDigest: response.plan_digest,
                catalogRevision: response.catalog_revision,
              }
              : candidate
          )),
        }));
      }
      await invalidatePackMutationSurfaces(get);
      completeMutation(mutationKey, mutation.requestId);
      get().addToast('Pack approved.', 'success');
    } catch (error) {
      if (responseAccepted || isMutationResultUnknown(error)) {
        const unknown = markMutationUnknown(mutationKey, mutation.requestId);
        set((current) => ({
          packMutationUnknown: {...current.packMutationUnknown, [mutationKey]: unknown},
        }));
        let reconciled: Awaited<ReturnType<typeof reconcilePackApprovalStatus>> | null = null;
        try {
          reconciled = await reconcilePackApprovalStatus(unknown, get);
        } catch (error) {
          recordClientDiagnostic({
            code: 'pack.mutation.reconciliation_failed',
            operation: 'approve.pack',
            error,
          });
        }
        if (reconciled?.state === 'succeeded') {
          set((current) => {
            const next = {...current.packMutationUnknown};
            delete next[mutationKey];
            return {packMutationUnknown: next};
          });
          get().addToast('Pack approval reconciled by the Host.', 'success');
          return;
        }
        if (reconciled?.state === 'failed') {
          set((current) => {
            const next = {...current.packMutationUnknown};
            delete next[mutationKey];
            return {packMutationUnknown: next};
          });
          const code = reconciled.status.safe_error_code ?? 'PACK_APPROVAL_FAILED';
          const failure = new Error(`Pack approval was denied or failed (${code}).`);
          get().addToast(failure.message, 'error');
          throw failure;
        }
        get().addToast(MUTATION_UNKNOWN_MESSAGE, 'error');
        throw new MutationResultUnknownError(mutationKey, mutation.requestId);
      }
      completeMutation(mutationKey, mutation.requestId);
      const message = error instanceof Error ? error.message : 'Failed to approve pack';
      get().addToast(message, 'error');
      throw error;
    } finally {
      set((current) => {
        const pending = {...current.packApprovalPending};
        delete pending[id];
        return {packApprovalPending: pending};
      });
    }
  },

  revokePackApproval: async (id) => {
    const state = get();
    const pack = state.packs.find((candidate) => candidate.id === id);
    if (
      !pack
      || !pack.installed
      || !pack.approved
      || pack.type === 'core'
      || pack.required
      || state.packApprovalPending[id]
    ) return;

    const mutationKey = `pack:revoke:${id}`;
    const mutation = beginMutation(mutationKey, {
      kind: 'pack.revoke',
      pack_id: id,
      expected_approved: false,
      expected_enabled: false,
      operation_id: 'approval.revoke',
      contract_id: PACK_CONTROL_CONTRACT,
      contract_map_digest: PINNED_FRONTEND_CONTRACT_MAP_ARTIFACT_DIGEST,
    });
    const version = beginPackMutation(id);
    set((current) => ({
      packApprovalPending: {...current.packApprovalPending, [id]: true},
    }));
    let responseAccepted = false;
    try {
      const response = await apiRevokePackApproval(id, {requestId: mutation.requestId});
      if (
        response.pack_id !== id
        || response.approved
        || response.approval_status !== 'revoked'
        || response.enabled !== false
      ) {
        throw new Error('Tobkiri did not confirm Pack approval revocation.');
      }
      responseAccepted = true;
      if (packMutationVersions.get(id) === version) {
        set((current) => ({
          packs: current.packs.map((candidate) => (
            candidate.id === id
              ? {
                ...candidate,
                enabled: false,
                approved: false,
                approvalStatus: 'revoked',
                approvalReason: 'approval_revoked',
                approvalIssues: ['approval_revoked'],
                profileId: response.profile_id,
                workspaceId: response.workspace_id,
                profileRevision: response.profile_revision,
                planDigest: response.plan_digest,
                catalogRevision: response.catalog_revision,
              }
              : candidate
          )),
        }));
      }
      await invalidatePackMutationSurfaces(get);
      completeMutation(mutationKey, mutation.requestId);
      get().addToast('Pack approval revoked.', 'success');
    } catch (error) {
      if (responseAccepted || isMutationResultUnknown(error)) {
        const unknown = markMutationUnknown(mutationKey, mutation.requestId);
        set((current) => ({
          packMutationUnknown: {...current.packMutationUnknown, [mutationKey]: unknown},
        }));
        let reconciled: Awaited<ReturnType<typeof reconcilePackMutationStatus>> | null = null;
        try {
          reconciled = await reconcilePackMutationStatus(
            unknown,
            get,
            'approval.revoke',
            (status) => status.state === 'succeeded' && packMutationSuccess(unknown, get),
          );
        } catch (error) {
          recordClientDiagnostic({
            code: 'pack.mutation.reconciliation_failed',
            operation: 'revoke.pack.approval',
            error,
          });
        }
        if (reconciled?.state === 'succeeded') {
          set((current) => {
            const next = {...current.packMutationUnknown};
            delete next[mutationKey];
            return {packMutationUnknown: next};
          });
          get().addToast('Pack approval revocation reconciled by the Host.', 'success');
          return;
        }
        if (reconciled?.state === 'failed') {
          set((current) => {
            const next = {...current.packMutationUnknown};
            delete next[mutationKey];
            return {packMutationUnknown: next};
          });
          const code = reconciled.status.safe_error_code ?? 'PACK_REVOKE_FAILED';
          const failure = new Error(`Pack approval revocation was denied or failed (${code}).`);
          get().addToast(failure.message, 'error');
          throw failure;
        }
        get().addToast(MUTATION_UNKNOWN_MESSAGE, 'error');
        throw new MutationResultUnknownError(mutationKey, mutation.requestId);
      }
      completeMutation(mutationKey, mutation.requestId);
      const message = error instanceof Error ? error.message : 'Failed to revoke Pack approval';
      get().addToast(message, 'error');
      throw error;
    } finally {
      set((current) => {
        const pending = {...current.packApprovalPending};
        delete pending[id];
        return {packApprovalPending: pending};
      });
    }
  },

  togglePack: async (id) => {
    const state = get();
    const pack = state.packs.find((candidate) => candidate.id === id);
    if (!pack || !pack.installed || !pack.approved || pack.required || state.packTogglePending[id]) return false;

    const expectedEnabled = !pack.enabled;
    const mutationKey = `pack:toggle:${id}:${expectedEnabled ? 'enable' : 'disable'}`;
    if (state.packMutationUnknown[mutationKey]) {
      get().addToast(MUTATION_UNKNOWN_MESSAGE, 'error');
      return false;
    }
    const mutation = beginMutation(mutationKey, {
      kind: 'pack.toggle',
      pack_id: id,
      expected_enabled: expectedEnabled,
      operation_id: pack.enabled ? 'pack.disable' : 'pack.enable',
      contract_id: PACK_CONTROL_CONTRACT,
      contract_map_digest: PINNED_FRONTEND_CONTRACT_MAP_ARTIFACT_DIGEST,
    });
    const version = beginPackMutation(id);
    set((current) => ({
      packTogglePending: {...current.packTogglePending, [id]: true},
    }));
    let responseAccepted = false;

    try {
      const response = pack.enabled
        ? await apiDisablePack(id, {requestId: mutation.requestId})
        : await apiEnablePack(id, {requestId: mutation.requestId});
      if (response.pack_id !== id || response.enabled !== expectedEnabled) {
        throw new Error('Tobkiri did not confirm the requested Pack state.');
      }
      responseAccepted = true;
      if (packMutationVersions.get(id) === version) {
        set((current) => ({
          packs: current.packs.map((candidate) => (
            candidate.id === id
              ? {
                ...candidate,
                enabled: response.enabled,
                profileId: response.profile_id,
                workspaceId: response.workspace_id,
                profileRevision: response.profile_revision,
                planDigest: response.plan_digest,
                catalogRevision: response.catalog_revision,
              }
              : candidate
          )),
        }));
      }
      await invalidatePackMutationSurfaces(get);
      completeMutation(mutationKey, mutation.requestId);
      return true;
    } catch (error) {
      if (responseAccepted || isMutationResultUnknown(error)) {
        const unknown = markMutationUnknown(mutationKey, mutation.requestId);
        set((current) => ({
          packMutationUnknown: {...current.packMutationUnknown, [mutationKey]: unknown},
        }));
        let reconciled: Awaited<ReturnType<typeof reconcilePackMutationStatus>> | null = null;
        try {
          reconciled = await reconcilePackMutationStatus(
            unknown,
            get,
            pack.enabled ? 'pack.disable' : 'pack.enable',
            (status) => status.state === 'succeeded' && packMutationSuccess(unknown, get),
          );
        } catch (error) {
          recordClientDiagnostic({
            code: 'pack.mutation.reconciliation_failed',
            operation: 'toggle.pack',
            error,
          });
        }
        if (reconciled?.state === 'succeeded') {
          set((current) => {
            const next = {...current.packMutationUnknown};
            delete next[mutationKey];
            return {packMutationUnknown: next};
          });
          return true;
        }
        if (reconciled?.state === 'failed') {
          set((current) => {
            const next = {...current.packMutationUnknown};
            delete next[mutationKey];
            return {packMutationUnknown: next};
          });
          get().addToast(`Pack toggle was denied or failed (${reconciled.status.safe_error_code ?? 'PACK_TOGGLE_FAILED'}).`, 'error');
          return false;
        }
        get().addToast(MUTATION_UNKNOWN_MESSAGE, 'error');
        return false;
      }
      completeMutation(mutationKey, mutation.requestId);
      if (packMutationVersions.get(id) === version) {
        set((current) => ({
          packs: current.packs.map((candidate) => (
            candidate.id === id ? {...candidate, enabled: pack.enabled} : candidate
          )),
        }));
      }
      const message = error instanceof Error ? error.message : 'Failed to toggle pack';
      get().addToast(message, 'error');
      return false;
    } finally {
      set((current) => {
        const pending = {...current.packTogglePending};
        delete pending[id];
        return {packTogglePending: pending};
      });
    }
  },

  profile: readLocalProfile(),
  updateLocalProfile: (profileUpdate) => {
    set((state) => {
      const profile = {...state.profile, ...profileUpdate, connected: state.profile.connected};
      writeLocalStorage(PROFILE_STORAGE_KEY, JSON.stringify({
        avatar: profile.avatar,
        username: profile.username,
        language: profile.language,
        job: profile.job,
      }));
      return {profile};
    });
  },
}));
