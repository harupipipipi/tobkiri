import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import {act, StrictMode} from 'react';
import {createRoot} from 'react-dom/client';
import {JSDOM} from 'jsdom';
import {MemoryRouter} from 'react-router';
import {clearApiPrefetchCache, setRuntimeDispatchStatus} from '@/src/lib/api';
import type {ApiPresentationState} from '@/src/lib/apiTypes';
import {PROFILE_REGISTRY_API_VERSION} from '@/src/lib/profileRegistry';
import {useAppStore} from '@/src/store';
import {Setup} from './Setup';

const setupFixture = JSON.parse(readFileSync(new URL(
  '../../../../tobkiri_runtime/tobkiri_protocol/fixtures/defaults_setup_v4.canonical.json',
  import.meta.url,
), 'utf8'));

const desktopPresentation: ApiPresentationState = {
  catalog: {
    schema: 'io.tobkiri.launcher.presentation-catalog.v1',
    generator: 'test',
    generator_version: '1.0.0',
    default_profile_id: 'defaults',
    default_profile_source: 'profiles/defaults',
    default_profile_digest: 'sha256:profile',
    default_selection: {base_pack_id: 'defaults-basepack', shell_provider_id: 'shell.tauri.default'},
    contract_revisions: [],
    source_manifest_digests: {},
    base_packs: [],
    shell_providers: [],
    generated_at: 1,
  },
  selection: null,
  materialization: {
    status: 'not_selected',
    base_pack_id: null,
    shell_provider_id: null,
    selected_contributions: [],
    artifact: null,
    reason: null,
  },
};

function apiResult(data: unknown): Response {
  return new Response(JSON.stringify({success: true, data}), {
    headers: {'Content-Type': 'application/json'},
  });
}

function apiError(error: string, status: number): Response {
  return new Response(JSON.stringify({success: false, error}), {
    status,
    headers: {'Content-Type': 'application/json'},
  });
}

async function mountSetup(options: {
  activeProfileId?: string | null;
  setupRoute?: () => Promise<unknown>;
  nativeReview?: (...args: unknown[]) => Promise<unknown>;
  afterNativeReview?: () => Response;
  handover?: (command: string) => Promise<unknown>;
  deferSecondSetup?: boolean;
  registryFails?: boolean;
  runtimeStatus?: 'runtime_ready' | 'profile_reconfirmation_required';
  strictMode?: boolean;
  reconcileRuntime?: boolean;
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
  const commands: string[] = [];
  Object.defineProperty(dom.window, '__TAURI__', {
    value: {core: {invoke: async (command: string, ...args: unknown[]) => {
      commands.push(command);
      if (command === 'native_setup_route') return options.setupRoute ? options.setupRoute() : 'legacy';
      if (command === 'start_native_setup_review') return options.nativeReview?.(...args);
      if (command === 'get_presentation_catalog') return desktopPresentation;
      return options.handover ? options.handover(command) : {stage: 'completed'};
    }}},
    configurable: true,
  });
  const requests: Array<{url: string; method: string}> = [];
  let setupReads = 0;
  let releaseSetupReload: (() => void) | undefined;
  Object.defineProperty(globalThis, 'fetch', {configurable: true, value: async (url: string, init?: RequestInit) => {
    requests.push({url: String(url), method: init?.method ?? 'GET'});
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
    if (String(url).includes('/api/setup/packs/install')) {
      return apiError('activation conflict', 409);
    }
    if (String(url).includes('/api/setup/packs')) {
      setupReads += 1;
      if (options.afterNativeReview && commands.includes('start_native_setup_review')) {
        return options.afterNativeReview();
      }
      const setupResponse = () => apiResult(setupFixture);
      if (options.deferSecondSetup && setupReads === 2) {
        return new Promise<Response>((resolve) => { releaseSetupReload = () => resolve(setupResponse()); });
      }
      return setupResponse();
    }
    if (options.reconcileRuntime && url === '/api/setup/runtime/reconcile') {
      return apiResult({});
    }
    throw new Error(`Unexpected request: ${url}`);
  }});
  clearApiPrefetchCache();
  const runtimeStatus = options.runtimeStatus ?? 'runtime_ready';
  setRuntimeDispatchStatus(runtimeStatus);
  useAppStore.setState({runtimeStatus});
  if (options.reconcileRuntime) {
    useAppStore.setState({
      refreshRuntimeHealth: async () => undefined,
      refreshPackVMDoctor: async () => ({
        ready: false,
        backend_id: 'macos-vz',
        platform: 'macos-arm64',
        instance: 'tobkiri-packvm-v4',
        reason: 'provisioning_required',
        attestation_digest: null,
      }),
      loadPacks: async () => undefined,
      packsError: null,
    });
  }
  const container = dom.window.document.getElementById('root')!;
  const root = createRoot(container);
  await act(async () => {
    const setup = <MemoryRouter><Setup /></MemoryRouter>;
    root.render(options.strictMode ? <StrictMode>{setup}</StrictMode> : setup);
  });
  await act(async () => { await new Promise((resolve) => setTimeout(resolve, 25)); });
  return {
    container,
    dom,
    root,
    commands,
    requests,
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
      .find((button) => button.textContent?.includes('Activate Tobkiri Harness'));
    assert.ok(activateWhileBusy, 'Harness activation control remains visible during migration');
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
      .find((button) => button.textContent?.includes('Activate Tobkiri Harness'));
    assert.ok(activate, 'Harness activation control remains visible while activation is fenced');
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
      .find((button) => button.textContent?.includes('Activate Tobkiri Harness'));
    assert.ok(activate, 'Harness activation control remains visible while activation is fenced');
    assert.equal(activate.disabled, true);
  } finally {
    await ui.cleanup();
  }
});

function setupButton(container: HTMLElement, label: RegExp): HTMLButtonElement {
  const button = [...container.querySelectorAll('button')].find((element) => label.test(element.textContent ?? ''));
  assert.ok(button, `Missing Setup button ${label}`);
  return button;
}

test('desktop legacy activation requires the explicit Host route', async () => {
  let resolveRoute!: (route: string) => void;
  let route = new Promise<string>((resolve) => { resolveRoute = resolve; });
  const ui = await mountSetup({activeProfileId: 'existing', setupRoute: () => route});
  try {
    const confirm = ui.container.querySelector<HTMLInputElement>('input[type="checkbox"]')!;
    const activate = setupButton(ui.container, /^Activate Tobkiri Harness$/);
    assert.equal(confirm.disabled, true);
    await act(async () => { resolveRoute('unknown-route'); });
    assert.equal(confirm.disabled, true);
    await act(async () => {
      route = Promise.resolve('legacy');
      setupButton(ui.container, /Check desktop Setup again/).click();
    });
    assert.equal(confirm.disabled, false);
    await act(async () => { confirm.click(); });
    await act(async () => { activate.click(); });
    assert.equal(ui.requests.filter((request) => request.url.includes('/install')).length, 1);
    assert.ok(!ui.commands.includes('start_native_setup_review'));
  } finally {
    await ui.cleanup();
  }
});

test('native Setup waits for the fresh-start choice and opens once while the parent anchor stays mounted', async () => {
  let publish!: (reply: {state: string}) => void;
  const ui = await mountSetup({
    strictMode: true,
    setupRoute: async () => 'native',
    nativeReview: (...args) => {
      assert.deepEqual(args, []);
      return new Promise((resolve) => { publish = resolve; });
    },
    afterNativeReview: () => apiResult({...setupFixture, state: 'active'}),
    reconcileRuntime: true,
  });
  try {
    assert.ok(!ui.commands.includes('start_native_setup_review'));
    await act(async () => { ui.container.querySelector<HTMLInputElement>('input[type="radio"]')!.click(); });
    const anchor = ui.container.querySelector('[data-native-setup-review-anchor]');
    assert.ok(anchor);
    assert.equal(anchor.getAttribute('data-native-setup-review-anchor'),
      setupFixture.recommended_default_profile.confirmation.confirmation_digest);
    const confirm = ui.container.querySelector<HTMLInputElement>('input[type="checkbox"]')!;
    const activate = setupButton(ui.container, /^Activate Tobkiri Harness$/);
    const migrate = ui.container.querySelectorAll<HTMLInputElement>('input[type="radio"]')[1]!;
    assert.equal(confirm.disabled, true);
    assert.equal(activate.disabled, true);
    assert.equal(migrate.disabled, true);
    await act(async () => {
      confirm.click();
      activate.click();
      migrate.click();
    });
    assert.equal(ui.container.querySelector('[data-native-setup-review-anchor]'), anchor);
    assert.equal(ui.commands.filter((command) => command === 'start_native_setup_review').length, 1);
    assert.ok(!ui.requests.some((request) => request.url.includes('/install')));

    await act(async () => { publish({state: 'published'}); });
    assert.equal(ui.container.querySelector('[data-native-setup-review-anchor]'), null);
    assert.match(ui.container.textContent ?? '', /Choose a Base Pack, then its Shell/);
    assert.equal(ui.requests.filter((request) => request.url === '/api/setup/runtime/reconcile').length, 1);
    assert.ok(!ui.requests.some((request) => request.url.includes('/install')));
  } finally {
    await ui.cleanup();
  }
});

test('native Setup waits for migration and the refreshed authoritative review', async () => {
  const ui = await mountSetup({
    setupRoute: async () => 'native',
    nativeReview: async () => ({state: 'unavailable'}),
    deferSecondSetup: true,
  });
  try {
    const migrate = ui.container.querySelectorAll<HTMLInputElement>('input[type="radio"]')[1]!;
    await act(async () => { migrate.click(); });
    assert.ok(!ui.commands.includes('start_native_setup_review'));
    await act(async () => { setupButton(ui.container, /Review previous Host data/).click(); });
    assert.ok(!ui.commands.includes('start_native_setup_review'));
    await act(async () => { ui.releaseSetupReload(); });
    assert.equal(ui.commands.filter((command) => command === 'start_native_setup_review').length, 1);
  } finally {
    await ui.cleanup();
  }
});

for (const state of ['unavailable', 'rejected']) {
  test(`native Setup ${state} allows a new review with a fresh anchor`, async () => {
    const diagnostic = 'Harness Profile <em>candidate</em> changed';
    let response = JSON.stringify({error: diagnostic});
    const ui = await mountSetup({
      activeProfileId: 'existing',
      setupRoute: async () => 'native',
      nativeReview: async () => state === 'rejected'
        ? {state, response} : {state},
    });
    try {
      assert.equal(ui.commands.filter((command) => command === 'start_native_setup_review').length, 1);
      const anchor = ui.container.querySelector('[data-native-setup-review-anchor]');
      assert.ok(anchor);
      if (state === 'rejected') {
        assert.ok(ui.container.textContent?.includes(diagnostic));
        assert.equal(ui.container.querySelector('[role="alert"] em'), null);
      }
      const activate = setupButton(ui.container, /^Activate Tobkiri Harness$/);
      await act(async () => { activate.click(); });
      await act(async () => { useAppStore.setState({runtimeStatus: 'profile_reconfirmation_required'}); });
      assert.equal(ui.container.querySelector('[data-native-setup-review-anchor]'), null);
      await act(async () => { useAppStore.setState({runtimeStatus: 'runtime_ready'}); });
      assert.equal(ui.commands.filter((command) => command === 'start_native_setup_review').length, 1);
      const previousAnchor = ui.container.querySelector('[data-native-setup-review-anchor]');
      assert.ok(previousAnchor);
      response = JSON.stringify({error: 'x'.repeat(513)});
      await act(async () => { setupButton(ui.container, /Refresh desktop Setup review/).click(); });
      const freshAnchor = ui.container.querySelector('[data-native-setup-review-anchor]');
      assert.ok(freshAnchor);
      assert.notEqual(freshAnchor, previousAnchor);
      assert.equal(ui.commands.filter((command) => command === 'start_native_setup_review').length, 2);
      assert.equal(ui.commands.filter((command) => command === 'native_setup_route').length, 1);
      assert.ok(ui.requests.every((request) => request.method === 'GET'));
      if (state === 'rejected') {
        assert.match(ui.container.textContent ?? '', /Harness Profile activation was rejected/);
      }
    } finally {
      await ui.cleanup();
    }
  });
}

for (const loseReply of [false, true]) {
  test(`native Setup verifies with reads after ${loseReply ? 'a lost IPC reply' : 'an uncertain result'}`, async () => {
    const ui = await mountSetup({
      activeProfileId: 'existing',
      setupRoute: async () => 'native',
      nativeReview: async () => {
        if (loseReply) throw new Error('Native IPC disconnected');
        return {state: 'verification_required'};
      },
      afterNativeReview: () => apiError('verification unavailable', 503),
    });
    try {
      assert.match(ui.container.textContent ?? '', /Activation was submitted; verification is required/);
      assert.doesNotMatch(ui.container.textContent ?? '', /Refresh desktop Setup review/);
      const setupReadsBeforeRetry = ui.setupReads;
      await act(async () => { setupButton(ui.container, /Verify activation/).click(); });
      assert.ok(ui.setupReads > setupReadsBeforeRetry);
      assert.equal(ui.commands.filter((command) => command === 'start_native_setup_review').length, 1);
      assert.ok(ui.requests.every((request) => request.method === 'GET'));
    } finally {
      await ui.cleanup();
    }
  });
}

test('leaving Setup ignores a late native completion', async () => {
  let publish!: (reply: {state: string}) => void;
  const ui = await mountSetup({
    activeProfileId: 'existing',
    setupRoute: async () => 'native',
    nativeReview: () => new Promise((resolve) => { publish = resolve; }),
  });
  try {
    assert.equal(ui.commands.filter((command) => command === 'start_native_setup_review').length, 1);
    const requestsBeforeUnmount = ui.requests.length;
    await act(async () => { ui.root.render(null); });
    await act(async () => { publish({state: 'published'}); });
    assert.equal(ui.requests.length, requestsBeforeUnmount);
  } finally {
    await ui.cleanup();
  }
});
