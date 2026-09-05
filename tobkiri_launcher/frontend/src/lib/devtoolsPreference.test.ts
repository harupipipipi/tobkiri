import assert from 'node:assert/strict';
import test from 'node:test';

import {normalizeDevtoolsEnabled} from './devtoolsPreference';

test('Devtools defaults off and accepts only the explicit persisted value', () => {
  assert.equal(normalizeDevtoolsEnabled(null), false);
  assert.equal(normalizeDevtoolsEnabled('false'), false);
  assert.equal(normalizeDevtoolsEnabled('1'), false);
  assert.equal(normalizeDevtoolsEnabled('TRUE'), false);
  assert.equal(normalizeDevtoolsEnabled('true'), true);
});
