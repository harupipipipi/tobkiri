import type {GeneratedFrontendContractRoute} from './generatedFrontendContractMap';

const HOST_PROFILE_CONTROL_ROUTES = new Map([
  ['GET /api/runtime-surface/profiles', 'profile.catalog.read'],
  ['GET /api/runtime-surface/operation-status', 'operation.status.read'],
  ['POST /api/runtime-surface/profile-change/resolve', 'profile.change.resolve'],
  ['POST /api/runtime-surface/profile-change/review', 'profile.change.review'],
  ['POST /api/runtime-surface/profile-change/approve', 'profile.change.approve'],
  ['POST /api/runtime-surface/profile-change/activate', 'profile.change.activate'],
]);

/** Classify only the verified Host ceremony routes available before activation. */
export function isHostProfileControlRoute(route: GeneratedFrontendContractRoute): boolean {
  const operation = HOST_PROFILE_CONTROL_ROUTES.get(`${route.method} ${route.path}`);
  if (!operation || route.targets.length !== 1) return false;
  const target = route.targets[0];
  return target.operation_id === operation
    && target.contract_id === 'tobkiri.host.control-presentation.v4'
    && target.provider_id === 'tobkiri.host.control-presentation'
    && target.function_id === 'tobkiri.host.control-presentation';
}
