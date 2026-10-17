import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import {act} from 'react';
import {createRoot} from 'react-dom/client';
import {JSDOM} from 'jsdom';
import {MemoryRouter} from 'react-router';
import {clearApiPrefetchCache, setRuntimeDispatchStatus} from '@/src/lib/api';
import {PROFILE_REGISTRY_API_VERSION} from '@/src/lib/profileRegistry';
import {useAppStore} from '@/src/store';
import {Setup} from './Setup';

const setupFixture = JSON.parse(readFileSync(new URL(
  '../../../../tobkiri_runtime/tobkiri_protocol/fixtures/defaults_setup_v4.canonical.json',
  import.meta.url,
), 'utf8'));

async function mountSetup(options: {
  activeProfileId?: string | null;
  handover?: (command: string) => Promise<unknown>;
  deferSecondSetup?: boolean;
  registryFails?: boolean;
  runtimeStatus?: 'runtime_ready' | 'profile_reconfirmation_required';
}) {
  const dom = new JSDOM('<div id="root"></div>', {url: 'http://localhost/panel/setup'});
  const previousState = useAppStore.getState();
  const descriptors = Object.getOwnPropertyDescriptors(globalThis);
  Object.defineProperties(globalThis, {
    window: {value: dom.window, configurable: true},
    document: {value: dom.window.document, configurable: true},
    navigator: {value: dom.window.navigator, configurable: true},
    localStorage: {value: dom.window.localStorage, configurable: true},
    IS_REACT_ACT_ENVIRONMENT: {value: true, configurable: true},
  });
  Object.defineProperty(dom.window, '__TAURI_INTERNALS__', {value: {}, configurable: true});
  Object.defineProperty(dom.window, '__TAURI__', {value: {core: {invoke: async (command: string) => (
    options.handover ? options.handover(command) : {stage: 'completed'}
  )}}, configurable: true});
  let setupReads = 0;
  let releaseSetupReload: (() => void) | undefined;
  Object.defineProperty(globalThis, 'fetch', {configurable: true, value: async (url: string) => {
    if (String(url).includes('/api/v4/profiles')) {
      if (options.registryFails) {
        return new Response(JSON.stringify({success: false, error: 'registry unavailable'}), {status: 503});
      }
      return new Response(JSON.stringify({success: true, data: {
        profile_registry_api_version: PROFILE_REGISTRY_API_VERSION,
        generation: 1,
        active_profile_id: options.activeProfileId ?? null,
        active_profile_revision: options.activeProfileId ? `sha256:${'a'.repeat(64)}` : null,
        profiles: options.activeProfileId ? [{
          profile_id: options.activeProfileId,
          profile_revision: `sha256:${'a'.repeat(64)}`,
          profile: {profile_id: options.activeProfileId, display_name: 'Active'},
          order: 0, parent_revision: null, tombstone: false,
          created_at: 1, updated_at: 1, legacy_ids: [],
        }] : [],
      }}), {headers: {'Content-Type': 'application/json'}});
    }
    if (String(url).includes('/api/setup/packs')) {
      setupReads += 1;
      const setupResponse = () => new Response(JSON.stringify({success: true, data: setupFixture}), {
        headers: {'Content-Type': 'application/json'},
      });
      if (options.deferSecondSetup && setupReads === 2) {
        return new Promise<Response>((resolve) => { releaseSetupReload = () => resolve(setupResponse()); });
      }
      return setupResponse();
    }
    throw new Error(`Unexpected request: ${url}`);
  }});
  clearApiPrefetchCache();
  const runtimeStatus = options.runtimeStatus ?? 'runtime_ready';
  setRuntimeDispatchStatus(runtimeStatus);
  useAppStore.setState({runtimeStatus});
  const container = dom.window.document.getElementById('root')!;
  const root = createRoot(container);
  await act(async () => { root.render(<MemoryRouter><Setup /></MemoryRouter>); });
  await act(async () => { await new Promise((resolve) => setTimeout(resolve, 25)); });
  return {
    container,
    dom,
    root,
    get setupReads() { return setupReads; },
    releaseSetupReload() { releaseSetupReload?.(); },
    async cleanup() {
      await act(async () => { root.unmount(); });
      useAppStore.setState(previousState, true);
      for (const key of ['window', 'document', 'navigator', 'localStorage', 'fetch', 'IS_REACT_ACT_ENVIRONMENT']) {
        if (descriptors[key]) Object.defineProperty(globalThis, key, descriptors[key]);
        else Reflect.deleteProperty(globalThis, key);
      }
      dom.window.close();
    },
  };
}

test('inactive Setup offers an explicit migration choice and reloads after native success', async () => {
  let resolveHandover!: (result: unknown) => void;
  const ui = await mountSetup({
    handover: () => new Promise((resolve) => { resolveHandover = resolve; }),
    deferSecondSetup: true,
  });
  try {
    assert.match(ui.container.textContent ?? '', /Choose how to start this Profile/);
    const radios = [...ui.container.querySelectorAll<HTMLInputElement>('input[type="radio"]')];
    assert.equal(radios.length, 2);
    await act(async () => { radios[1]!.click(); });
    assert.match(ui.container.textContent ?? '', /Review previous Host data/);
    assert.equal(ui.container.querySelector<HTMLButtonElement>('button')?.disabled, false);
    const reviewMigration = [...ui.container.querySelectorAll<HTMLButtonElement>('button')]
      .find((button) => button.textContent?.includes('Review previous Host data'))!;
    await act(async () => { reviewMigration.click(); });
    const activateWhileBusy = [...ui.container.querySelectorAll<HTMLButtonElement>('button')]
      .find((button) => button.textContent?.includes('Activate Defaults Profile'))!;
    assert.equal(activateWhileBusy.disabled, true, 'activation stays locked during the native ceremony');
    await act(async () => { resolveHandover({stage: 'completed'}); });
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 25)); });
    assert.match(ui.container.textContent ?? '', /Data imported/);
    const nativeReview = [...ui.container.querySelectorAll<HTMLButtonElement>('button')]
      .find((button) => button.textContent?.includes('Review previous Host data'))!;
    assert.equal(nativeReview.disabled, true, 'the terminal state stays mounted during authoritative reload');
    ui.releaseSetupReload();
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 25)); });
    assert.ok(ui.setupReads >= 2, 'successful migration reloads authoritative Setup state');
    assert.equal(ui.container.querySelector<HTMLInputElement>('input[type="checkbox"]')?.checked, false,
      'migration success requires a fresh review confirmation');
  } finally {
    await ui.cleanup();
  }
});

test('a failed migration cannot retain review consent or unlock activation', async () => {
  const ui = await mountSetup({handover: async () => { throw new Error('native migration failed'); }});
  try {
    const radios = [...ui.container.querySelectorAll<HTMLInputElement>('input[type="radio"]')];
    await act(async () => { radios[0]!.click(); });
    const review = ui.container.querySelector<HTMLInputElement>('input[type="checkbox"]');
    assert.ok(review);
    await act(async () => { review.click(); });
    await act(async () => { radios[1]!.click(); });
    const reviewMigration = [...ui.container.querySelectorAll<HTMLButtonElement>('button')]
      .find((button) => button.textContent?.includes('Review previous Host data'))!;
    await act(async () => { reviewMigration.click(); });
    assert.match(ui.container.textContent ?? '', /native migration failed/);
    assert.equal(ui.container.querySelector<HTMLInputElement>('input[type="checkbox"]')?.checked, false);
    const activate = [...ui.container.querySelectorAll<HTMLButtonElement>('button')]
      .find((button) => button.textContent?.includes('Activate Defaults Profile'))!;
    assert.equal(activate.disabled, true);
  } finally {
    await ui.cleanup();
  }
});

test('a canceled native picker safely reopens the explicit fresh-start choice', async () => {
  const ui = await mountSetup({handover: async () => null});
  try {
    const radios = [...ui.container.querySelectorAll<HTMLInputElement>('input[type="radio"]')];
    await act(async () => { radios[1]!.click(); });
    const reviewMigration = [...ui.container.querySelectorAll<HTMLButtonElement>('button')]
      .find((button) => button.textContent?.includes('Review previous Host data'))!;
    await act(async () => { reviewMigration.click(); });
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 25)); });
    const freshChoice = ui.container.querySelector<HTMLInputElement>('input[type="radio"]');
    assert.ok(freshChoice);
    assert.equal(freshChoice.disabled, false);
    assert.equal(ui.container.querySelector<HTMLInputElement>('input[type="checkbox"]')?.checked, false);
  } finally {
    await ui.cleanup();
  }
});

test('canceling recovery after a migration error cannot clear the activation fence', async () => {
  const ui = await mountSetup({handover: async (command) => {
    if (command === 'handover_previous_development_host') throw new Error('migration outcome is uncertain');
    return null;
  }});
  try {
    const radios = [...ui.container.querySelectorAll<HTMLInputElement>('input[type="radio"]')];
    await act(async () => { radios[1]!.click(); });
    const reviewMigration = [...ui.container.querySelectorAll<HTMLButtonElement>('button')]
      .find((button) => button.textContent?.includes('Review previous Host data'))!;
    await act(async () => { reviewMigration.click(); });
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 25)); });
    const recover = [...ui.container.querySelectorAll<HTMLButtonElement>('button')]
      .find((button) => button.textContent?.includes('Check migration or review recovery'))!;
    await act(async () => { recover.click(); });
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 25)); });
    const freshChoice = ui.container.querySelector<HTMLInputElement>('input[type="radio"]');
    assert.ok(freshChoice);
    assert.equal(freshChoice.disabled, true);
  } finally {
    await ui.cleanup();
  }
});

test('Setup hides native migration when the authoritative registry has an active Profile', async () => {
  const ui = await mountSetup({activeProfileId: 'existing'});
  try {
    assert.doesNotMatch(ui.container.textContent ?? '', /Choose how to start this Profile/);
    assert.doesNotMatch(ui.container.textContent ?? '', /Review previous Host data/);
  } finally {
    await ui.cleanup();
  }
});

test('Setup hides native migration during Profile reconfirmation', async () => {
  const ui = await mountSetup({runtimeStatus: 'profile_reconfirmation_required'});
  try {
    assert.doesNotMatch(ui.container.textContent ?? '', /Choose how to start this Profile/);
    assert.doesNotMatch(ui.container.textContent ?? '', /Review previous Host data/);
  } finally {
    await ui.cleanup();
  }
});

test('an unavailable Profile registry keeps migration and activation locked with a retry', async () => {
  const ui = await mountSetup({registryFails: true});
  try {
    assert.match(ui.container.textContent ?? '', /Host Profile registry could not be verified/);
    assert.match(ui.container.textContent ?? '', /Retry Host Profile verification/);
    assert.doesNotMatch(ui.container.textContent ?? '', /Choose how to start this Profile/);
    const activate = [...ui.container.querySelectorAll<HTMLButtonElement>('button')]
      .find((button) => button.textContent?.includes('Activate Defaults Profile'))!;
    assert.equal(activate.disabled, true);
  } finally {
    await ui.cleanup();
  }
});
