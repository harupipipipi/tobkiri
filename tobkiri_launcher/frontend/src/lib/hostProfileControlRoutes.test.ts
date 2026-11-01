import assert from 'node:assert/strict';
import {test} from 'node:test';

import {
  generatedRouteFor,
  VERIFIED_GENERATED_FRONTEND_CONTRACT_MAP,
} from './generatedFrontendContractMap';
import {isHostProfileControlRoute} from './hostProfileControlRoutes';

test('pre-activation classification rejects changed identities and ambiguous targets', () => {
  const route = generatedRouteFor(
    VERIFIED_GENERATED_FRONTEND_CONTRACT_MAP,
    'GET',
    '/api/runtime-surface/profiles',
  );
  assert.equal(isHostProfileControlRoute(route), true);
  for (const field of ['contract_id', 'provider_id', 'function_id', 'operation_id'] as const) {
    const changed = structuredClone(route);
    changed.targets[0][field] += '.untrusted';
    assert.equal(isHostProfileControlRoute(changed), false, field);
  }
  for (const changed of [
    {...route, method: 'POST' as const},
    {...route, path: `${route.path}/other`},
    {...route, path: '/api/runtime-surface/settings'},
    {...route, targets: []},
    {...route, targets: [...route.targets, ...route.targets]},
  ]) assert.equal(isHostProfileControlRoute(changed), false);
});
