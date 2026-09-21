import type {ApiDashboard, ApiPack} from './apiTypes';
import type {DashboardData, Pack, PackOperation} from '../store';

export function transformPack(api: ApiPack): Pack {
  const declaredOperations = api.declared_operations ?? api.operations ?? api.invokable_operations ?? [];
  const invokableOperationKeys = new Set(
    (api.invokable_operations ?? []).map((operation) => (
      `${operation.contract_id}:${operation.operation_id}`
    )),
  );
  const operations: PackOperation[] = declaredOperations.map((operation) => ({
    operationId: operation.operation_id,
    contractId: operation.contract_id,
    providerId: operation.provider_id,
    capabilities: operation.capabilities ?? operation.required_capabilities ?? [],
    inputSchema: operation.input_schema ?? {},
    invokable: operation.invokable === true
      || invokableOperationKeys.has(`${operation.contract_id}:${operation.operation_id}`),
  }));
  return {
    id: api.pack_id,
    name: api.name,
    version: api.version,
    type: api.is_core ? 'core' : 'community',
    required: api.required === true,
    installed: api.installed,
    enabled: api.enabled,
    description: api.description,
    artifactDigest: api.artifact_digest,
    packArtifactDigest: api.pack_artifact_digest ?? null,
    profileId: api.profile_id,
    workspaceId: api.workspace_id,
    profileRevision: api.profile_revision,
    planDigest: api.plan_digest,
    catalogRevision: api.catalog_revision,
    approvalStatus: api.approval_status || (api.approved ? 'approved' : 'unknown'),
    approvalReason: api.approval_reason ?? null,
    approved: api.approved ?? api.is_core,
    hashValid: api.hash_valid ?? (api.is_core ? true : null),
    criticalChanged: api.critical_changed ?? (api.is_core ? false : null),
    approvalIssues: api.approval_issues || [],
    capabilities: (api.capabilities ?? []).map((capability) => ({
      name: capability.name,
      description: capability.description ?? '',
    })),
    operations,
    flows: api.flows ?? operations.map((operation) => operation.operationId),
    dependencies: api.dependencies ?? [],
  };
}

export function transformPacks(apiPacks: ApiPack[]): Pack[] {
  return apiPacks.map(transformPack);
}

export function formatUptime(seconds: number | null): string {
  if (seconds === null || seconds === undefined) return '--';
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  return `${hours}h ${minutes}m`;
}

export function transformDashboard(api: ApiDashboard): DashboardData {
  return {
    kernelStatus: api.kernel.status === 'running'
      ? 'running'
      : api.kernel.status === 'error'
        ? 'error'
        : 'stopped',
    uptime: formatUptime(api.kernel.uptime),
    activePacks: api.packs.enabled,
    registeredFlows: api.flows.total,
    activities: [],
    supervisor: api.supervisor ?? null,
  };
}
