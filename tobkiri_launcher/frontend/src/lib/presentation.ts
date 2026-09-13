import type {
  ApiBasePackDescriptor,
  ApiDynamicFrontendCatalog,
  ApiFrontendContribution,
  ApiPresentationCatalog,
  ApiPresentationMaterialization,
  ApiPresentationSelection,
  ApiShellProviderDescriptor,
} from './apiTypes';

export const SHELL_CONTRACT_ID = 'app.shell.v1';

export const DYNAMIC_FRONTEND_CATALOG_VERSION = 'rumi.ui.contribution.v1';
export const CONVERSATION_VIEW_TYPE = 'conversation_v4';
export const CONVERSATION_ACTION_CONTRACT = 'conversation.turn.v1';
export const CONVERSATION_OPERATION_ID = 'complete';
const SHA256_DIGEST = /^sha256:[0-9a-f]{64}$/;

export interface VerifiedViewCapabilityQuery {
  viewType: string;
  actionContract?: string;
  operationId?: string;
}

function nonEmpty(value: string | null | undefined): boolean {
  return typeof value === 'string' && value.trim().length > 0;
}

function hasUniqueContributionIds(
  contributions: ApiFrontendContribution[],
): boolean {
  const ids = contributions.map((contribution) => contribution.contribution_id);
  return ids.every(nonEmpty) && new Set(ids).size === ids.length;
}

/**
 * Check the catalog envelope before exposing any dynamic view capability.
 * Unknown or ambiguous contribution rows invalidate the catalog so a stale
 * or colliding row cannot become a launchable UI by accident.
 */
export function isVerifiedDynamicFrontendCatalog(
  catalog: ApiDynamicFrontendCatalog | null,
): catalog is ApiDynamicFrontendCatalog {
  return Boolean(
    catalog
    && catalog.version === DYNAMIC_FRONTEND_CATALOG_VERSION
    && nonEmpty(catalog.profile_id)
    && SHA256_DIGEST.test(catalog.profile_revision)
    && nonEmpty(catalog.activation_id)
    && SHA256_DIGEST.test(catalog.plan_hash)
    && SHA256_DIGEST.test(catalog.catalog_hash)
    && Array.isArray(catalog.contributions)
    && hasUniqueContributionIds(catalog.contributions)
    && Array.isArray(catalog.quarantined_pack_ids)
    && catalog.quarantined_pack_ids.every(nonEmpty),
  );
}

function isVerifiedContributionBinding(
  contribution: ApiFrontendContribution,
  catalog: ApiDynamicFrontendCatalog,
  query: VerifiedViewCapabilityQuery,
): boolean {
  return nonEmpty(contribution.contribution_id)
    && nonEmpty(contribution.owner_pack_id)
    && contribution.kind === 'route'
    && contribution.mode === 'declarative'
    && nonEmpty(contribution.route)
    && nonEmpty(contribution.action_contract)
    && nonEmpty(contribution.operation_id)
    && nonEmpty(contribution.provider_id)
    && nonEmpty(contribution.function_id)
    && nonEmpty(contribution.build_identity)
    && SHA256_DIGEST.test(contribution.owner_pack_hash ?? '')
    && SHA256_DIGEST.test(contribution.descriptor_hash ?? '')
    && contribution.resolved_profile_id === catalog.profile_id
    && contribution.resolved_profile_revision === catalog.profile_revision
    && contribution.resolved_activation_id === catalog.activation_id
    && contribution.resolved_plan_hash === catalog.plan_hash
    && contribution.view?.type === query.viewType
    && (!query.actionContract || contribution.action_contract === query.actionContract)
    && (!query.operationId || contribution.operation_id === query.operationId)
    && !catalog.quarantined_pack_ids.includes(contribution.owner_pack_id);
}

/**
 * Return verified route/view capabilities matching the requested semantic
 * contract. The owner, provider, route, artifact hashes, and profile/plan
 * binding remain data supplied by the accepted catalog.
 */
export function verifiedViewCapabilities(
  catalog: ApiDynamicFrontendCatalog | null,
  query: VerifiedViewCapabilityQuery,
): ApiFrontendContribution[] {
  if (!isVerifiedDynamicFrontendCatalog(catalog) || !nonEmpty(query.viewType)) return [];
  return catalog.contributions.filter((contribution) => (
    isVerifiedContributionBinding(contribution, catalog, query)
  ));
}

/** Resolve one unambiguous capability, failing closed on collisions. */
export function resolveVerifiedViewCapability(
  catalog: ApiDynamicFrontendCatalog | null,
  query: VerifiedViewCapabilityQuery,
): ApiFrontendContribution | null {
  const matches = verifiedViewCapabilities(catalog, query);
  return matches.length === 1 ? matches[0] : null;
}

/**
 * Conversation is a semantic capability, not a product-owned route. Its
 * presence is optional for every Profile and never gates bootstrap or Shell
 * launch readiness.
 */
export function isConversationCapabilityReady(
  catalog: ApiDynamicFrontendCatalog | null,
): boolean {
  return resolveVerifiedViewCapability(catalog, {
    viewType: CONVERSATION_VIEW_TYPE,
    actionContract: CONVERSATION_ACTION_CONTRACT,
    operationId: CONVERSATION_OPERATION_ID,
  }) !== null;
}

/** Resolve the optional conversation view only for the Profile that owns it. */
export function resolveConversationCapabilityForProfile(
  catalog: ApiDynamicFrontendCatalog | null,
  profileId: string,
): ApiFrontendContribution | null {
  if (!nonEmpty(profileId) || catalog?.profile_id !== profileId) return null;
  return resolveVerifiedViewCapability(catalog, {
    viewType: CONVERSATION_VIEW_TYPE,
    actionContract: CONVERSATION_ACTION_CONTRACT,
    operationId: CONVERSATION_OPERATION_ID,
  });
}

export function verifiedCapabilityLabel(
  capability: ApiFrontendContribution,
): string {
  return capability.label.trim() || capability.view?.type?.trim() || 'Verified capability';
}

export interface PresentationCompatibility {
  compatible: boolean;
  reasons: string[];
}

export function findBasePack(
  catalog: ApiPresentationCatalog,
  basePackId: string,
): ApiBasePackDescriptor | null {
  return catalog.base_packs.find((basePack) => basePack.pack_id === basePackId) ?? null;
}

export function findShellProvider(
  catalog: ApiPresentationCatalog,
  providerId: string,
): ApiShellProviderDescriptor | null {
  return catalog.shell_providers.find((provider) => provider.provider_id === providerId) ?? null;
}

export function checkShellCompatibility(
  basePack: ApiBasePackDescriptor | null,
  shell: ApiShellProviderDescriptor | null,
): PresentationCompatibility {
  const reasons: string[] = [];
  if (!basePack) {
    reasons.push('The selected Base Pack is unavailable.');
  }
  if (!shell) {
    reasons.push('The selected Shell Provider is unavailable.');
  }
  if (!basePack || !shell) {
    return {compatible: false, reasons};
  }

  if (shell.contract_id !== SHELL_CONTRACT_ID) {
    reasons.push(`The provider implements ${shell.contract_id}, not ${SHELL_CONTRACT_ID}.`);
  }
  if (!basePack.allowed_families.includes(shell.presentation_family)) {
    reasons.push(
      `${shell.display_name} is not allowed for the ${basePack.display_name} presentation family.`,
    );
  }

  const providedCapabilities = new Set(shell.capabilities);
  const missingCapabilities = basePack.required_capabilities.filter(
    (capability) => !providedCapabilities.has(capability),
  );
  if (missingCapabilities.length > 0) {
    reasons.push(`Missing required capabilities: ${missingCapabilities.join(', ')}.`);
  }

  return {compatible: reasons.length === 0, reasons};
}

export function compatibleShellProviders(
  catalog: ApiPresentationCatalog,
  basePackId: string,
): ApiShellProviderDescriptor[] {
  const basePack = findBasePack(catalog, basePackId);
  return catalog.shell_providers.filter(
    (shell) => checkShellCompatibility(basePack, shell).compatible,
  );
}

export function defaultPresentationSelection(
  catalog: ApiPresentationCatalog,
): ApiPresentationSelection | null {
  const selection = catalog.default_selection;
  if (!selection) return null;
  const basePack = findBasePack(catalog, selection.base_pack_id);
  const shell = findShellProvider(catalog, selection.shell_provider_id);
  return checkShellCompatibility(basePack, shell).compatible ? selection : null;
}

export function normalizePresentationSelection(
  catalog: ApiPresentationCatalog,
  selection: ApiPresentationSelection | null,
): ApiPresentationSelection | null {
  if (!selection) return defaultPresentationSelection(catalog);
  const compatible = compatibleShellProviders(catalog, selection.base_pack_id);
  if (compatible.some((shell) => shell.provider_id === selection.shell_provider_id)) {
    return selection;
  }
  return defaultPresentationSelection(catalog);
}

export function selectShellAfterBaseChange(
  catalog: ApiPresentationCatalog,
  basePackId: string,
  currentShellId: string,
): ApiPresentationSelection | null {
  const compatible = compatibleShellProviders(catalog, basePackId);
  const shell = compatible.find((candidate) => candidate.provider_id === currentShellId)
    ?? (catalog.default_selection.base_pack_id === basePackId
      ? compatible.find(
        (candidate) => candidate.provider_id === catalog.default_selection.shell_provider_id,
      )
      : undefined);
  return shell
    ? {base_pack_id: basePackId, shell_provider_id: shell.provider_id}
    : null;
}

export function materializationLabel(materialization: ApiPresentationMaterialization): string {
  if (materialization.status === 'materialized') return 'Materialized';
  if (materialization.status === 'blocked') return 'Blocked';
  return 'Not selected';
}

export function materializationReason(materialization: ApiPresentationMaterialization): string {
  if (materialization.reason) return materialization.reason;
  if (materialization.status === 'materialized') {
    return 'The selected Shell contribution set is ready for a verified production launch.';
  }
  if (materialization.status === 'not_selected') {
    return 'Choose a Base Pack and a compatible Shell Provider.';
  }
  return 'The selected presentation cannot be launched until its production artifact is verified.';
}

export function launchDisabledReason(
  materialization: ApiPresentationMaterialization,
): string | null {
  if (materialization.status === 'materialized') return null;
  return materializationReason(materialization);
}

export function launchDisabledReasonForSelection(
  materialization: ApiPresentationMaterialization,
  savedSelection: ApiPresentationSelection | null,
  currentSelection: ApiPresentationSelection | null,
): string | null {
  if (
    !savedSelection
    || !currentSelection
    || savedSelection.base_pack_id !== currentSelection.base_pack_id
    || savedSelection.shell_provider_id !== currentSelection.shell_provider_id
  ) {
    return 'Save the current Base Pack and Shell selection before launching.';
  }
  return launchDisabledReason(materialization);
}

export function approvalLabel(state: string): string {
  switch (state) {
    case 'verified':
      return 'Verified';
    case 'not_required':
      return 'No approval required';
    case 'pending':
      return 'Approval pending';
    case 'blocked':
      return 'Blocked';
    default:
      return 'Approval unavailable';
  }
}

export function authorityLabel(authorityMode: string): string {
  switch (authorityMode) {
    case 'lease_only':
      return 'Brokered lease only';
    case 'os_entitlement':
      return 'OS entitlement';
    case 'none':
      return 'No Host authority';
    default:
      return 'Authority mode unavailable';
  }
}
