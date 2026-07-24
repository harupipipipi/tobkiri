import assert from 'node:assert/strict';
import test from 'node:test';

import {
  buildProfileCreationRequest,
  canLoadDashboardProfiles,
  copyTextToClipboard,
  launchStartupProfileFromDashboard,
} from './Dashboard';
import type {StartupProfileMutationResponseData} from '@/src/lib/apiTypes';

function launchResponse(restartRequested: boolean): StartupProfileMutationResponseData {
  return {
    profile: {
      version: 3,
      profile_id: 'p1',
      name: 'Default',
      base_pack: 'defaultspack',
      graph_id: 'defaultspack.startup',
      graph_ports: [],
      packs: ['defaultspack'],
      node_overrides: {},
      created_at: 1,
      updated_at: 1,
    },
    launched: true,
    restart_requested: restartRequested,
  };
}

test('Dashboard starts loading profiles while runtime health is still settling', () => {
  assert.equal(canLoadDashboardProfiles(false, 'starting'), true);
  assert.equal(canLoadDashboardProfiles(false, 'panel_ready'), true);
  assert.equal(canLoadDashboardProfiles(true, 'runtime_ready'), true);
  assert.equal(canLoadDashboardProfiles(false, 'error'), false);
});

test('profile creation requires an explicit base-pack selection', () => {
  assert.equal(buildProfileCreationRequest('Draft', ''), null);
  assert.deepEqual(
    buildProfileCreationRequest('  My profile  ', '  defaultspack  '),
    {name: 'My profile', base_pack: 'defaultspack'},
  );
});

test('copyTextToClipboard copies the complete runtime error message', async () => {
  let copied = '';
  const success = await copyTextToClipboard('Kernel failed to start', {
    writeText: async (text: string) => {
      copied = text;
    },
  });

  assert.equal(success, true);
  assert.equal(copied, 'Kernel failed to start');
});

test('launch opens Defaultspack desktop after a restart handoff', async () => {
  const calls: string[] = [];
  await launchStartupProfileFromDashboard({
    profileId: 'p1',
    preferredProfileId: 'p1',
    launchProfile: async (profileId) => {
      calls.push(`launch:${profileId}`);
      return launchResponse(true);
    },
    refreshProfiles: async (profileId) => {
      calls.push(`refresh-profiles:${profileId}`);
    },
    refreshDashboard: async () => {
      calls.push('refresh-dashboard');
    },
    openDesktop: async () => {
      calls.push('open-desktop');
    },
    setSuccessFeedback: (message) => {
      calls.push(`success:${message}`);
    },
    setErrorFeedback: (message) => {
      calls.push(`error:${message}`);
    },
    translateError: (error) => error instanceof Error ? error.message : 'translated',
  });

  assert.deepEqual(calls, [
    'launch:p1',
    'refresh-profiles:p1',
    'refresh-dashboard',
    'open-desktop',
    'success:Profile launched. Defaultspack window opened.',
  ]);
});

test('launch opens Defaultspack desktop immediately when no restart is requested', async () => {
  const calls: string[] = [];
  await launchStartupProfileFromDashboard({
    profileId: 'p1',
    preferredProfileId: 'p1',
    launchProfile: async (profileId) => {
      calls.push(`launch:${profileId}`);
      return launchResponse(false);
    },
    refreshProfiles: async (profileId) => {
      calls.push(`refresh-profiles:${profileId}`);
    },
    refreshDashboard: async () => {
      calls.push('refresh-dashboard');
    },
    openDesktop: async () => {
      calls.push('open-desktop');
    },
    setSuccessFeedback: (message) => {
      calls.push(`success:${message}`);
    },
    setErrorFeedback: (message) => {
      calls.push(`error:${message}`);
    },
    translateError: (error) => error instanceof Error ? error.message : 'translated',
  });

  assert.deepEqual(calls, [
    'launch:p1',
    'refresh-profiles:p1',
    'refresh-dashboard',
    'open-desktop',
    'success:Profile launched. Defaultspack window opened.',
  ]);
});
