import assert from 'node:assert/strict';
import {afterEach, test} from 'node:test';

import {
  admitSignedPackFromFolder, checkLauncherUpdate, fetchSignedPackAdmissionStatus,
  onboardSignedPackFromFolder, openLauncherUpdateRelease,
} from './desktopHost';

const originalWindow = globalThis.window;

afterEach(() => {
  Object.defineProperty(globalThis, 'window', {
    configurable: true,
    value: originalWindow,
  });
});

test('Launcher update bridge uses only the dedicated native commands', async () => {
  const invocations: Array<{command: string; args?: Record<string, unknown>}> = [];
  Object.defineProperty(globalThis, 'window', {
    configurable: true,
    value: {
      __TAURI__: {
        core: {
          invoke: async (command: string, args?: Record<string, unknown>) => {
            invocations.push({command, args});
            if (command === 'check_launcher_update') {
              return {
                state: 'available',
                current_version: '1.0.0',
                latest_version: '1.1.0',
              };
            }
            return undefined;
          },
        },
      },
    },
  });

  assert.deepEqual(await checkLauncherUpdate(), {
    state: 'available',
    current_version: '1.0.0',
    latest_version: '1.1.0',
  });
  await openLauncherUpdateRelease();

  assert.deepEqual(invocations, [
    {command: 'check_launcher_update', args: undefined},
    {command: 'open_launcher_update_release', args: undefined},
  ]);
});

test('Launcher update bridge fails closed outside the desktop shell', async () => {
  Object.defineProperty(globalThis, 'window', {
    configurable: true,
    value: {},
  });
  await assert.rejects(checkLauncherUpdate(), /only available in Tobkiri Launcher/);
  await assert.rejects(openLauncherUpdateRelease(), /only available in Tobkiri Launcher/);
});

test('signed Pack picker never passes a renderer path to the native command', async () => {
  const invocations: Array<{command: string; args?: Record<string, unknown>}> = [];
  Object.defineProperty(globalThis, 'window', {
    configurable: true,
    value: {
      __TAURI__: {
        core: {
          invoke: async (command: string, args?: Record<string, unknown>) => {
            invocations.push({command, args});
            return {pack_id: 'sample.signed', artifact_digest: 'sha256:abc', publisher_id: 'sample', catalog_refreshed: true};
          },
        },
      },
    },
  });

  assert.equal((await admitSignedPackFromFolder())?.pack_id, 'sample.signed');
  assert.deepEqual(invocations, [{command: 'admit_signed_pack_from_folder', args: undefined}]);
});

test('publisher onboarding keeps both selected paths in the native command', async () => {
  const invocations: Array<{command: string; args?: Record<string, unknown>}> = [];
  Object.defineProperty(globalThis, 'window', {
    configurable: true,
    value: {
      __TAURI__: {
        core: {
          invoke: async (command: string, args?: Record<string, unknown>) => {
            invocations.push({command, args});
            return {pack_id: 'qa.frontend.input', artifact_digest: 'sha256:abc', publisher_id: 'publisher.qa.frontend', catalog_refreshed: true};
          },
        },
      },
    },
  });
  assert.equal((await onboardSignedPackFromFolder())?.pack_id, 'qa.frontend.input');
  assert.deepEqual(invocations, [{command: 'onboard_signed_pack_from_folder', args: undefined}]);
});

test('signed Pack trust status uses the native bridge without renderer paths', async () => {
  const invocations: string[] = [];
  Object.defineProperty(globalThis, 'window', {
    configurable: true,
    value: {
      __TAURI__: {
        core: {
          invoke: async (command: string) => {
            invocations.push(command);
            return {ready: false, onboarding_supported: true};
          },
        },
      },
    },
  });
  assert.deepEqual(await fetchSignedPackAdmissionStatus(), {ready: false, onboarding_supported: true});
  assert.deepEqual(invocations, ['signed_pack_admission_status']);
});
