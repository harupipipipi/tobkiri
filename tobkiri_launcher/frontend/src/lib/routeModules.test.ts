import assert from 'node:assert/strict';
import test from 'node:test';

import {loadRouteModuleWithSessionRecovery} from './routeModules';

function moduleImportFailure(): Error {
  return new TypeError('Importing a module script failed.');
}

test('loadRouteModuleWithSessionRecovery returns the module without recovery on success', async () => {
  const expected = {default: () => null};
  let loaderCalls = 0;
  let recoverCalls = 0;

  const result = await loadRouteModuleWithSessionRecovery(
    async () => {
      loaderCalls += 1;
      return expected;
    },
    async () => {
      recoverCalls += 1;
      return true;
    },
  );

  assert.equal(result, expected);
  assert.equal(loaderCalls, 1);
  assert.equal(recoverCalls, 0);
});

test('loadRouteModuleWithSessionRecovery retries the loader once after session recovery', async () => {
  const expected = {default: () => null};
  let loaderCalls = 0;
  let recoverCalls = 0;

  const result = await loadRouteModuleWithSessionRecovery(
    async () => {
      loaderCalls += 1;
      if (loaderCalls === 1) throw moduleImportFailure();
      return expected;
    },
    async () => {
      recoverCalls += 1;
      return true;
    },
  );

  assert.equal(result, expected);
  assert.equal(loaderCalls, 2);
  assert.equal(recoverCalls, 1);
});

test('loadRouteModuleWithSessionRecovery rethrows when session recovery is unavailable', async () => {
  const failure = moduleImportFailure();
  let loaderCalls = 0;
  let recoverCalls = 0;

  await assert.rejects(
    loadRouteModuleWithSessionRecovery(
      async () => {
        loaderCalls += 1;
        throw failure;
      },
      async () => {
        recoverCalls += 1;
        return false;
      },
    ),
    (error: unknown) => error === failure,
  );

  assert.equal(loaderCalls, 1);
  assert.equal(recoverCalls, 1);
});

test('loadRouteModuleWithSessionRecovery retries at most once', async () => {
  const retryFailure = new TypeError('Importing a module script failed.');
  let loaderCalls = 0;
  let recoverCalls = 0;

  await assert.rejects(
    loadRouteModuleWithSessionRecovery(
      async () => {
        loaderCalls += 1;
        throw retryFailure;
      },
      async () => {
        recoverCalls += 1;
        return true;
      },
    ),
    (error: unknown) => error === retryFailure,
  );

  assert.equal(loaderCalls, 2);
  assert.equal(recoverCalls, 1);
});
