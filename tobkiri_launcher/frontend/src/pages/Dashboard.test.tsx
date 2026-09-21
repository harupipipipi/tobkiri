import assert from 'node:assert/strict';
import test from 'node:test';

import {
  canLoadDashboardProfiles,
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

test('Dashboard can load profiles once the panel is ready', () => {
  assert.equal(canLoadDashboardProfiles(false, 'starting'), false);
  assert.equal(canLoadDashboardProfiles(false, 'panel_ready'), true);
  assert.equal(canLoadDashboardProfiles(true, 'runtime_ready'), true);
});

test('launch skips immediate Defaultspack desktop open during restart handoff', async () => {
  const calls: string[] = [];
  await launchStartupProfileFromDashboard({
    profileId: 'p1',
    preferredProfileId: 'p1',
    launchProfile: async (profileId) => {
      calls.push(`launch:${profileId}`);
      return launchResponse(true);
    },
    refreshProfiles: async () => {
      calls.push('refresh-profiles');
    },
    refreshDashboard: async () => {
      calls.push('refresh-dashboard');
    },
    openDesktop: async () => {
      calls.push('open-desktop');
    },
    queueDesktopAfterRestart: async () => {
      calls.push('queue-desktop-after-restart');
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
    'queue-desktop-after-restart',
    'success:Profile launched. Defaultspack will open after the runtime restart is ready.',
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
    queueDesktopAfterRestart: async () => {
      calls.push('queue-desktop-after-restart');
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
