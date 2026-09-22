import type {
  ApiBasePackDescriptor,
  ApiDynamicFrontendCatalog,
  ApiPresentationCatalog,
  ApiPresentationMaterialization,
  ApiPresentationSelection,
  ApiShellProviderDescriptor,
} from './apiTypes';

export const SHELL_CONTRACT_ID = 'app.shell.v1';

const CONVERSATION_CONTRIBUTION_ID = 'defaults.conversation.complete';
const SHA256_DIGEST = /^sha256:[0-9a-f]{64}$/;

export function isConversationCapabilityReady(
  catalog: ApiDynamicFrontendCatalog | null,
): boolean {
  if (
    catalog?.version !== 'rumi.ui.contribution.v1'
    || !catalog.profile_id
    || !SHA256_DIGEST.test(catalog.profile_revision)
    || !SHA256_DIGEST.test(catalog.plan_hash)
    || !SHA256_DIGEST.test(catalog.catalog_hash)
    || catalog.quarantined_pack_ids.includes('defaultspack')
  ) {
    return false;
  }
  const matches = catalog.contributions.filter(
    (contribution) => contribution.contribution_id === CONVERSATION_CONTRIBUTION_ID,
  );
  if (matches.length !== 1) return false;
  const contribution = matches[0];
  return contribution.kind === 'route'
    && contribution.mode === 'declarative'
    && contribution.route === '/chat'
    && contribution.owner_pack_id === 'defaultspack'
    && contribution.action_contract === 'conversation.turn.v1'
    && contribution.operation_id === 'complete'
    && contribution.provider_id === 'defaultspack.conversation'
    && contribution.function_id === 'defaultspack.conversation'
    && contribution.build_identity === 'defaultspack.conversation'
    && SHA256_DIGEST.test(contribution.owner_pack_hash ?? '')
    && SHA256_DIGEST.test(contribution.descriptor_hash ?? '')
    && contribution.resolved_profile_revision === catalog.profile_revision
    && contribution.resolved_plan_hash === catalog.plan_hash
    && contribution.view?.type === 'conversation_v4';
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
