import assert from 'node:assert/strict';
import {afterEach, beforeEach, test} from 'node:test';
import {JSDOM} from 'jsdom';

import {
  cleanupConfirmationForInstance,
  formatPackVMBytes,
  normalizePackVMConsent,
  normalizePackVMDoctor,
  normalizePackVMOperation,
  normalizePackVMPlan,
  readPackVMOperationId,
  stopConfirmationForInstance,
  writePackVMOperationId,
} from './packvmLifecycle';
import {preparePackVM} from './api';
import type {ApiPackVMDoctor} from './apiTypes';
import {
  getBrowserStorage,
  readSafeStorageValue,
  writeSafeStorageValue,
} from './safeStorage';
import {useAppStore} from '@/src/store';
import {setRuntimeDispatchStatus} from '@/src/lib/runtimeDispatchGate';

const digest = (character: string) => `sha256:${character.repeat(64)}`;

const planPayload = {
  backend_id: 'tobkiri.python-pack-v4',
  instance: 'tobkiri-packvm-v4',
  limactl: '/Users/haru/.local/bin/limactl',
  launcher_reason: null,
  runtime_path_status: 'ready',
  architecture: 'arm64',
  image_source: 'https://cloud-images.ubuntu.com/jammy/20260807/jammy-server-cloudimg-arm64.img',
  image_digest: digest('a'),
  image_size_bytes: 703_594_496,
  image_download_required: true,
  config_digest: digest('b'),
  guest_runner_digest: digest('c'),
  host_build_digest: digest('d'),
  ceremony_nonce: 'nonce-1',
  plan_digest: digest('e'),
  confirmation: 'PROVISION tobkiri-packvm-v4 eeeeeeeeeeee',
};

const readyDoctor = {
  ready: true,
  backend_id: 'tobkiri.python-pack-v4',
  platform: 'macos',
  instance: 'tobkiri-packvm-v4',
  reason: null,
  attestation_digest: digest('f'),
};
const notReadyDoctor: ApiPackVMDoctor = {
  ...readyDoctor,
  ready: false,
  reason: 'PackVM has not completed explicit provisioning.',
  attestation_digest: null,
};

let dom: JSDOM | null = null;
let previousStore: ReturnType<typeof useAppStore.getState>;
let originalFetch: typeof fetch;

beforeEach(() => {
  setRuntimeDispatchStatus('runtime_ready');
  previousStore = useAppStore.getState();
  originalFetch = globalThis.fetch;
  dom = new JSDOM('<!doctype html><html><body></body></html>', {
    url: 'http://localhost/panel/',
  });
  Object.defineProperties(globalThis, {
    window: {value: dom.window, configurable: true},
    document: {value: dom.window.document, configurable: true},
    localStorage: {value: dom.window.localStorage, configurable: true},
    sessionStorage: {value: dom.window.sessionStorage, configurable: true},
  });
  writeSafeStorageValue(getBrowserStorage('session'), 'rumi-panel-csrf', 'csrf-test');
});

afterEach(() => {
  globalThis.fetch = originalFetch;
  useAppStore.setState(previousStore, true);
  dom?.window.close();
  dom = null;
});

test('PackVM plan normalization drops host paths while preserving pinned facts', () => {
  const plan = normalizePackVMPlan(planPayload);
  assert.equal(plan.plan_digest, digest('e'));
  assert.equal(plan.image_size_bytes, 703_594_496);
  assert.equal('limactl' in plan, false);
  assert.doesNotMatch(JSON.stringify(plan), /Users|limactl/);
});

test('PackVM plan normalization accepts only strict fail-closed unavailable evidence', () => {
  const unavailablePlan = normalizePackVMPlan({
    ...planPayload,
    launcher_reason: 'A production-signed PackVM helper is unavailable.',
    runtime_path_status: 'unsafe',
    image_source: 'unavailable',
    image_digest: digest('0'),
    image_download_required: false,
    config_digest: digest('0'),
    guest_runner_digest: digest('0'),
    host_build_digest: digest('0'),
  });
  assert.equal(unavailablePlan.image_source, 'unavailable');
  assert.equal(unavailablePlan.runtime_path_status, 'unsafe');
  assert.match(unavailablePlan.launcher_reason ?? '', /production-signed/);

  for (const invalid of [
    {runtime_path_status: 'ready'},
    {launcher_reason: null},
    {image_download_required: true},
    {image_digest: digest('a')},
    {config_digest: digest('b')},
    {guest_runner_digest: digest('c')},
    {host_build_digest: digest('d')},
  ]) {
    assert.throws(
      () => normalizePackVMPlan({...unavailablePlan, ...invalid}),
      /inconsistent PackVM (?:availability|unavailable-plan) evidence/,
    );
  }
});

test('PackVM normalization rejects tampered digests and missing success evidence', () => {
  assert.throws(
    () => normalizePackVMPlan({...planPayload, image_digest: digest('z').slice(0, -1)}),
    /invalid PackVM image_digest digest/,
  );
  assert.throws(
    () => normalizePackVMDoctor({...readyDoctor, attestation_digest: null}),
    /without an attestation digest/,
  );
  assert.throws(
    () => normalizePackVMOperation({
      operation_id: '11111111-1111-4111-8111-111111111111',
      operation_kind: 'provision',
      state: 'succeeded',
      plan_digest: digest('e'),
      updated_unix: 1,
    }),
    /without doctor evidence/,
  );
});

test('PackVM operation normalization preserves interrupted restart state and doctor evidence', () => {
  const interrupted = normalizePackVMOperation({
    operation_id: '11111111-1111-4111-8111-111111111111',
    operation_kind: 'provision',
    state: 'interrupted',
    plan_digest: digest('e'),
    updated_unix: 1,
  });
  assert.equal(interrupted.state, 'interrupted');

  const succeeded = normalizePackVMOperation({
    operation_id: '11111111-1111-4111-8111-111111111111',
    operation_kind: 'provision',
    state: 'succeeded',
    plan_digest: digest('e'),
    updated_unix: 2,
    doctor: readyDoctor,
  });
  assert.equal(succeeded.doctor?.attestation_digest, digest('f'));
});

test('PackVM consent normalization requires the typed one-shot evidence', () => {
  const consent = normalizePackVMConsent({
    consent_id: 'packvm-consent.abcdef',
    plan_digest: digest('e'),
    image_source: planPayload.image_source,
    image_digest: digest('a'),
    image_size_bytes: planPayload.image_size_bytes,
    image_download_approved: true,
  });
  assert.equal(consent.image_download_approved, true);
  assert.throws(
    () => normalizePackVMConsent({...consent, image_source: '/Users/haru/image.img'}),
    /invalid PackVM image_source/,
  );
});

test('PackVM operation identity uses durable local storage and rehydrates in a fresh browsing context', () => {
  writePackVMOperationId('11111111-1111-4111-8111-111111111111');
  assert.equal(readPackVMOperationId(), '11111111-1111-4111-8111-111111111111');
  assert.equal(
    readSafeStorageValue(getBrowserStorage('local'), 'tobkiri-launcher-packvm-operation'),
    '11111111-1111-4111-8111-111111111111',
  );

  const restarted = new JSDOM('<!doctype html><html><body></body></html>', {
    url: 'http://localhost/panel/',
  });
  const previousLocalStorage = globalThis.localStorage;
  const previousSessionStorage = globalThis.sessionStorage;
  Object.defineProperties(globalThis, {
    localStorage: {value: restarted.window.localStorage, configurable: true},
    sessionStorage: {value: restarted.window.sessionStorage, configurable: true},
  });
  try {
    // A separate JSDOM context does not share storage; copy the persisted Launcher record
    // to model the same-origin browsing context being recreated by Tauri.
    writeSafeStorageValue(
      getBrowserStorage('local'),
      'tobkiri-launcher-packvm-operation',
      '11111111-1111-4111-8111-111111111111',
    );
    assert.equal(readPackVMOperationId(), '11111111-1111-4111-8111-111111111111');
  } finally {
    Object.defineProperties(globalThis, {
      localStorage: {value: previousLocalStorage, configurable: true},
      sessionStorage: {value: previousSessionStorage, configurable: true},
    });
    restarted.window.close();
  }
});

test('PackVM operation storage clears a tampered non-canonical identity', () => {
  writeSafeStorageValue(getBrowserStorage('local'), 'tobkiri-launcher-packvm-operation', 'not-an-operation');
  assert.equal(readPackVMOperationId(), null);
  assert.equal(readSafeStorageValue(getBrowserStorage('local'), 'tobkiri-launcher-packvm-operation'), null);
});

test('PackVM API prepare uses the exact lifecycle route and request ceremony', async () => {
  let seenUrl = '';
  let seenHeaders: Headers | null = null;
  globalThis.fetch = (async (input, init) => {
    seenUrl = String(input);
    seenHeaders = new Headers(init?.headers);
    return new Response(JSON.stringify({success: true, data: planPayload}), {
      headers: {'Content-Type': 'application/json'},
    });
  }) as typeof fetch;

  const plan = await preparePackVM();
  assert.equal(seenUrl, '/api/v4/packvm/prepare');
  assert.equal(seenHeaders?.get('X-Rumi-CSRF'), 'csrf-test');
  assert.match(seenHeaders?.get('X-Tobkiri-Request-ID') ?? '', /^[0-9a-f-]{36}$/i);
  assert.equal(plan.image_source, planPayload.image_source);
  assert.equal('limactl' in plan, false);
});

test('PackVM API denial is surfaced and never treated as a plan', async () => {
  globalThis.fetch = (async () => new Response(JSON.stringify({
    success: false,
    data: {state: 'packvm_lifecycle_denied', operation: 'consent'},
    error: 'PackVM consent does not match a pending plan',
  }), {
    status: 409,
    headers: {'Content-Type': 'application/json'},
  })) as typeof fetch;

  await assert.rejects(preparePackVM(), /does not match a pending plan/);
});

test('PackVM cleanup confirmation stays bound to the authenticated instance', () => {
  assert.equal(
    stopConfirmationForInstance('tobkiri-packvm-v4'),
    'STOP tobkiri-packvm-v4',
  );
  assert.equal(
    cleanupConfirmationForInstance('tobkiri-packvm-v4'),
    'DELETE tobkiri-packvm-v4',
  );
  assert.equal(formatPackVMBytes(703_594_496), '671 MiB');
});

test('catalog stays blocked until healthy attestation and ignores a stale response', async () => {
  const dynamicCatalog = {
    version: 'rumi.ui.contribution.v1',
    profile_id: 'profile-a',
    profile_revision: digest('1'),
    plan_hash: digest('2'),
    contributions: [],
    diagnostics: [],
    quarantined_pack_ids: [],
    catalog_hash: digest('3'),
  };
  let catalogReads = 0;
  let releaseFirstResponse: ((response: Response) => void) | undefined;
  const firstResponse = new Promise<Response>((resolve) => {
    releaseFirstResponse = resolve;
  });
  globalThis.fetch = (async () => {
    catalogReads += 1;
    if (catalogReads === 1) return firstResponse;
    return new Response(JSON.stringify({success: true, data: {dynamic_host: dynamicCatalog}}), {
      headers: {'Content-Type': 'application/json'},
    });
  }) as typeof fetch;

  useAppStore.setState({
    packVmDoctor: notReadyDoctor,
    frontendCatalog: null,
    frontendCatalogError: null,
  });
  await useAppStore.getState().loadFrontendCatalog();
  assert.equal(catalogReads, 0);
  assert.equal(useAppStore.getState().frontendCatalog, null);

  useAppStore.setState({packVmDoctor: readyDoctor});
  const staleRequest = useAppStore.getState().loadFrontendCatalog();
  await new Promise((resolve) => setTimeout(resolve, 0));
  useAppStore.setState({packVmDoctor: notReadyDoctor});
  releaseFirstResponse?.(new Response(JSON.stringify({
    success: true,
    data: {dynamic_host: dynamicCatalog},
  }), {headers: {'Content-Type': 'application/json'}}));
  await staleRequest;
  assert.equal(useAppStore.getState().frontendCatalog, null);

  useAppStore.setState({packVmDoctor: readyDoctor});
  await useAppStore.getState().loadFrontendCatalog();
  assert.equal(catalogReads, 2);
  assert.deepEqual(useAppStore.getState().frontendCatalog, dynamicCatalog);

  useAppStore.setState({packVmDoctor: notReadyDoctor});
  await useAppStore.getState().loadFrontendCatalog();
  assert.equal(catalogReads, 2);
  assert.equal(useAppStore.getState().frontendCatalog, null);
});
