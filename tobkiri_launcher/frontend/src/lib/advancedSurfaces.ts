import {
  extractAuthoritativeInvokableOperationKeys,
  extractExactPackDescriptors,
  RUNTIME_CONTRACT_INVOKE_ACTION,
  type RuntimeSurfaceId,
  type RuntimeOperationDescriptor,
  type RuntimePackDescriptor,
  type RuntimeSurfaceEnvelope,
} from './runtimeSurface';

export type LauncherAdvancedViewId =
  | 'profile'
  | 'settings'
  | 'profileWiring'
  | 'profileFiles'
  | 'flow'
  | 'graph'
  | 'aiInput'
  | 'apiMap'
  | 'nodeManager';

export type LauncherViewSupport = 'rebuilt' | 'launcher_local' | 'mapped' | 'partial' | 'retired';

/**
 * The action vocabulary is intentionally separate from legacy write/read
 * labels. `contract_invoke` is the only generic Advanced-surface action that
 * can expose an operation invocation control.
 */
export type LauncherAdvancedAction =
  | 'local'
  | 'pack_lifecycle'
  | 'contract_invoke'
  | 'read_only'
  | 'none';

export type LauncherAdvancedCapability =
  | 'launcher_local'
  | 'runtime_projection'
  | 'pack_lifecycle'
  | 'contract_operation';

export interface LauncherAdvancedActionMetadata {
  capability: LauncherAdvancedCapability;
  action: LauncherAdvancedAction;
  label: string;
  sideEffects: string;
  approval: string;
  showContractInvocationUi: boolean;
  requiresAuthoritativeInvokableOperation: boolean;
}

/** Action metadata used by every Advanced descriptor and by the frame UI. */
export const LAUNCHER_ADVANCED_ACTION_METADATA: Record<
  LauncherAdvancedAction,
  LauncherAdvancedActionMetadata
> = {
  local: {
    capability: 'launcher_local',
    action: 'local',
    label: 'Launcher-local action',
    sideEffects: 'Changes Launcher-local presentation preferences only; the separate Profile change ceremony has its own digest and approval controls.',
    approval: 'Host approval is not required for local controls; the separate Profile change ceremony has its own Kernel approval step.',
    showContractInvocationUi: false,
    requiresAuthoritativeInvokableOperation: false,
  },
  pack_lifecycle: {
    capability: 'pack_lifecycle',
    action: 'pack_lifecycle',
    label: 'Pack lifecycle action',
    sideEffects: 'May change installed, enabled, or approved runtime Pack state.',
    approval: 'Pack approval and an authoritative Profile/Plan binding are required.',
    showContractInvocationUi: false,
    requiresAuthoritativeInvokableOperation: false,
  },
  contract_invoke: {
    capability: 'contract_operation',
    action: 'contract_invoke',
    label: 'Contract operation invoke',
    sideEffects: 'The provider may perform side effects declared by the Contract.',
    approval: 'Host approval is required before the operation can be invoked.',
    showContractInvocationUi: true,
    requiresAuthoritativeInvokableOperation: true,
  },
  read_only: {
    capability: 'runtime_projection',
    action: 'read_only',
    label: 'Read-only projection',
    sideEffects: 'No runtime side effects are exposed by this surface.',
    approval: 'No invocation approval applies because invocation is unavailable.',
    showContractInvocationUi: false,
    requiresAuthoritativeInvokableOperation: false,
  },
  none: {
    capability: 'runtime_projection',
    action: 'none',
    label: 'No action',
    sideEffects: 'No side effects are exposed by this surface.',
    approval: 'No action approval applies.',
    showContractInvocationUi: false,
    requiresAuthoritativeInvokableOperation: false,
  },
};

export interface LauncherAdvancedViewDescriptor {
  id: LauncherAdvancedViewId;
  label: string;
  support: LauncherViewSupport;
  sources: RuntimeSurfaceId[];
  summary: string;
  capability: LauncherAdvancedCapability;
  actions: LauncherAdvancedAction;
}

export interface AdvancedSurfaceActionState {
  status: string;
  stale: boolean;
  error: unknown | null;
}

export function advancedActionMetadata(
  descriptor: LauncherAdvancedViewDescriptor,
): LauncherAdvancedActionMetadata {
  return LAUNCHER_ADVANCED_ACTION_METADATA[descriptor.actions];
}

export function advancedActionAllowed(
  descriptor: LauncherAdvancedViewDescriptor,
  action: LauncherAdvancedAction,
): boolean {
  return advancedDescriptorMetadataParity(descriptor) && descriptor.actions === action;
}

export function advancedDescriptorMetadataParity(
  descriptor: LauncherAdvancedViewDescriptor,
): boolean {
  const metadata = advancedActionMetadata(descriptor);
  return metadata.action === descriptor.actions && metadata.capability === descriptor.capability;
}

/** Return the exact key used by the authoritative Packs projection. */
export function authoritativeOperationKey(
  contractId: string,
  operationId: string,
): string {
  return `${contractId}::${operationId}`;
}

function operationHasExactInvocationBinding(
  envelope: RuntimeSurfaceEnvelope<unknown>,
  operation: RuntimeOperationDescriptor,
  authoritativeKeys: ReadonlySet<string>,
  authoritativePacks: readonly RuntimePackDescriptor[],
): boolean {
  const operationKey = authoritativeOperationKey(operation.contract_id, operation.operation_id);
  const ownerPack = authoritativePacks.find((pack) => pack.pack_id === operation.owner_pack_id);
  return operation.action === RUNTIME_CONTRACT_INVOKE_ACTION
    && operation.invokable
    && authoritativeKeys.has(operationKey)
    && ownerPack !== undefined
    && ownerPack.enabled
    && ownerPack.approved
    && ownerPack.artifact_digest === operation.artifact_digest
    && ownerPack.invokable_operations.includes(operationKey)
    && operation.invocation_contribution_id !== null
    && operation.invocation_owner_pack_id === operation.owner_pack_id
    && operation.invocation_catalog_hash === envelope.catalog_revision
    && operation.catalog_digest === envelope.catalog_revision
    && typeof operation.activation_id === 'string'
    && operation.activation_id.length > 0;
}

/**
 * Select only operations that a descriptor is allowed to expose. The
 * authoritative Pack projection, the operation row, and the accepted catalog
 * digest must all agree before an invoke control is rendered.
 */
export function selectAdvancedContractInvokableOperations(
  descriptor: LauncherAdvancedViewDescriptor,
  state: AdvancedSurfaceActionState,
  envelope: RuntimeSurfaceEnvelope<unknown> | null,
  operations: RuntimeOperationDescriptor[],
  declaredOperationIds?: ReadonlySet<string>,
): RuntimeOperationDescriptor[] {
  if (
    !advancedDescriptorMetadataParity(descriptor)
    || !advancedActionAllowed(descriptor, RUNTIME_CONTRACT_INVOKE_ACTION)
    || !advancedActionMetadata(descriptor).showContractInvocationUi
    || !advancedActionMetadata(descriptor).requiresAuthoritativeInvokableOperation
    || state.status !== 'ready'
    || state.stale
    || state.error
    || !envelope
    || envelope.surface !== 'operations'
    || envelope.state !== 'ready'
  ) {
    return [];
  }
  const authoritativeKeys = extractAuthoritativeInvokableOperationKeys(envelope.data);
  if (!authoritativeKeys) return [];
  const authoritativePacks = extractExactPackDescriptors(envelope.data);
  if (authoritativePacks.length === 0) return [];
  return operations.filter((operation) => (
    (!declaredOperationIds || declaredOperationIds.has(operation.operation_id))
    && operationHasExactInvocationBinding(envelope, operation, authoritativeKeys, authoritativePacks)
  ));
}

export const LAUNCHER_ADVANCED_VIEWS: Record<LauncherAdvancedViewId, LauncherAdvancedViewDescriptor> = {
  profile: {
    id: 'profile',
    label: 'Profiles',
    support: 'rebuilt',
    sources: ['profile', 'profiles'],
    summary: 'Browse, configure, and activate verified Profiles; personal Launcher preferences remain clearly separate.',
    capability: 'launcher_local',
    actions: 'local',
  },
  settings: {
    id: 'settings',
    label: 'Settings',
    support: 'launcher_local',
    sources: ['settings'],
    summary: 'Theme, color mode, language, and avatar are presentation settings owned by Launcher.',
    capability: 'launcher_local',
    actions: 'local',
  },
  profileWiring: {
    id: 'profileWiring',
    label: 'Profile Wiring',
    support: 'mapped',
    sources: ['profile', 'principals', 'contracts'],
    summary: 'Search the exact ResolvedPlan connections between Function principals and Contract operations.',
    capability: 'runtime_projection',
    actions: 'read_only',
  },
  profileFiles: {
    id: 'profileFiles',
    label: 'Profile Files',
    support: 'mapped',
    sources: ['profile'],
    summary: 'Search the finite Profile artifacts and activation evidence published by the runtime.',
    capability: 'runtime_projection',
    actions: 'read_only',
  },
  flow: {
    id: 'flow',
    label: 'Flow',
    support: 'rebuilt',
    sources: ['operations'],
    summary: 'Contract-declared composition can invoke only an authoritative operation; provider side effects and Host approval are explicit.',
    capability: 'contract_operation',
    actions: 'contract_invoke',
  },
  graph: {
    id: 'graph',
    label: 'Graph',
    support: 'mapped',
    sources: ['profile'],
    summary: 'Explore the exact active Plan graph and follow its Profile connections.',
    capability: 'runtime_projection',
    actions: 'read_only',
  },
  aiInput: {
    id: 'aiInput',
    label: 'AI Input',
    support: 'rebuilt',
    sources: ['operations', 'contracts'],
    summary: 'Inputs are generated only for an authoritative Contract operation; provider side effects and Host approval are explicit.',
    capability: 'contract_operation',
    actions: 'contract_invoke',
  },
  apiMap: {
    id: 'apiMap',
    label: 'API & Route Map',
    support: 'mapped',
    sources: ['contracts', 'operations', 'principals'],
    summary: 'Search and filter the generated route, Contract, provider, and security map.',
    capability: 'runtime_projection',
    actions: 'read_only',
  },
  nodeManager: {
    id: 'nodeManager',
    label: 'Node Manager',
    support: 'mapped',
    sources: ['packs'],
    summary: 'Pack and Pack lifecycle projection using the existing verified catalog actions.',
    capability: 'pack_lifecycle',
    actions: 'pack_lifecycle',
  },
};

export const ADVANCED_VIEW_ORDER: LauncherAdvancedViewId[] = [
  'profile',
  'settings',
  'profileWiring',
  'profileFiles',
  'flow',
  'graph',
  'aiInput',
  'apiMap',
  'nodeManager',
];
