import assert from 'node:assert/strict';
import test from 'node:test';

import {
  activeTargetPackId,
  hasActiveSetupPackSelection,
  selectedSetupPackIds,
  setupPackSelectionUrl,
} from './setupPacks';

test('selectedSetupPackIds normalizes selected setup-pack ids', () => {
  assert.deepEqual(
    selectedSetupPackIds({ selected_setup_pack_ids: ['defaultspack', '', null, ' custom '] }),
    ['defaultspack', 'custom'],
  );
  assert.deepEqual(selectedSetupPackIds({ selected_setup_pack_ids: 'defaultspack' }), []);
  assert.deepEqual(selectedSetupPackIds(null), []);
});

test('active setup-pack selection requires an active target pack', () => {
  assert.equal(
    activeTargetPackId({ selected_setup_pack_ids: ['defaultspack'], active_target_pack_id: ' defaultspack ' }),
    'defaultspack',
  );
  assert.equal(
    hasActiveSetupPackSelection({ selected_setup_pack_ids: ['defaultspack'], active_target_pack_id: 'defaultspack' }),
    true,
  );
  assert.equal(
    hasActiveSetupPackSelection({ selected_setup_pack_ids: ['defaultspack'], active_target_pack_id: '' }),
    false,
  );
  assert.equal(
    hasActiveSetupPackSelection({ selected_setup_pack_ids: [], active_target_pack_id: 'defaultspack' }),
    false,
  );
});

test('setupPackSelectionUrl returns to panel setup for verification', () => {
  assert.equal(
    setupPackSelectionUrl(),
    '/setup?return_to=%2Fpanel%2Fsetup%3Fsetup_pack_done%3D1&color_mode=dark',
  );
  assert.equal(
    setupPackSelectionUrl('/panel/setup?setup_pack_done=1', 'light'),
    '/setup?return_to=%2Fpanel%2Fsetup%3Fsetup_pack_done%3D1&color_mode=light',
  );
});
