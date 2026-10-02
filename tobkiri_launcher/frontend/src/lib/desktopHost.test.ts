import assert from 'node:assert/strict';
import {afterEach, test} from 'node:test';

import {checkLauncherUpdate, openLauncherUpdateRelease} from './desktopHost';

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
