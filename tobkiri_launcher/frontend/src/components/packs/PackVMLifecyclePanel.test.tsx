import assert from 'node:assert/strict';
import {afterEach, beforeEach, test} from 'node:test';
import {act} from 'react';
import {createRoot, type Root} from 'react-dom/client';
import {JSDOM} from 'jsdom';

import type {ApiPackVMDoctor} from '@/src/lib/apiTypes';
import {
  getBrowserStorage,
  readSafeStorageValue,
  writeSafeStorageValue,
} from '@/src/lib/safeStorage';
import {useAppStore} from '@/src/store';
import {setRuntimeDispatchStatus} from '@/src/lib/runtimeDispatchGate';
import {PackVMLifecyclePanel} from './PackVMLifecyclePanel';

const digest = (character: string) => `sha256:${character.repeat(64)}`;
const operationId = '11111111-1111-4111-8111-111111111111';

const notReadyDoctor: ApiPackVMDoctor = {
  ready: false,
  backend_id: 'tobkiri.python-pack-v4',
  platform: 'macos',
  instance: 'tobkiri-packvm-v4',
  reason: 'PackVM has not completed explicit provisioning.',
  attestation_digest: null,
};

const healthyDoctor: ApiPackVMDoctor = {
  ready: true,
  backend_id: 'tobkiri.python-pack-v4',
  platform: 'macos',
  instance: 'tobkiri-packvm-v4',
  reason: null,
  attestation_digest: digest('f'),
};

const plan = {
  backend_id: 'tobkiri.python-pack-v4',
  instance: 'tobkiri-packvm-v4',
  launcher_reason: null,
  runtime_path_status: 'ready' as const,
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

const consent = {
  consent_id: 'packvm-consent.abcdef',
  plan_digest: plan.plan_digest,
  image_source: plan.image_source,
  image_digest: plan.image_digest,
  image_size_bytes: plan.image_size_bytes,
  image_download_approved: true,
};

function operation(
  state: 'queued' | 'cancelled' | 'interrupted' | 'succeeded' | 'failed',
  overrides = {},
) {
  return {
    operation_id: operationId,
    operation_kind: 'provision',
    state,
    plan_digest: plan.plan_digest,
    updated_unix: 1,
    ...(state === 'failed' ? {error: 'PackVM doctor failed'} : {}),
    ...(state === 'succeeded' ? {doctor: healthyDoctor} : {}),
    ...overrides,
  };
}

function createSurface(): {dom: JSDOM; container: HTMLElement; root: Root} {
  const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', {
    url: 'http://localhost/panel/packs/research-pack',
  });
  Object.defineProperties(globalThis, {
    window: {value: dom.window, configurable: true},
    document: {value: dom.window.document, configurable: true},
    navigator: {value: dom.window.navigator, configurable: true},
    localStorage: {value: dom.window.localStorage, configurable: true},
    sessionStorage: {value: dom.window.sessionStorage, configurable: true},
  });
  writeSafeStorageValue(getBrowserStorage('session'), 'rumi-panel-csrf', 'csrf-test');
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  const container = dom.window.document.querySelector<HTMLElement>('#root');
  assert.ok(container);
  return {dom, container, root: createRoot(container)};
}

function setDoctor(nextDoctor: ApiPackVMDoctor): void {
  useAppStore.setState({packVmDoctor: nextDoctor, packVmError: nextDoctor.ready ? null : nextDoctor.reason});
}

function configureStore(initialDoctor = notReadyDoctor): {
  setNextDoctor: (doctor: ApiPackVMDoctor) => void;
  refreshCalls: () => number;
} {
  let nextDoctor = initialDoctor;
  let refreshCount = 0;
  useAppStore.setState({
    packVmDoctor: initialDoctor,
    packVmDoctorLoading: false,
    packVmError: initialDoctor.ready ? null : initialDoctor.reason,
    frontendCatalog: null,
    frontendCatalogError: null,
    refreshPackVMDoctor: async () => {
      refreshCount += 1;
      setDoctor(nextDoctor);
      return nextDoctor;
    },
  });
  return {
    setNextDoctor: (doctor) => { nextDoctor = doctor; },
    refreshCalls: () => refreshCount,
  };
}

async function renderPanel(root: Root): Promise<void> {
  await act(async () => {
    root.render(<PackVMLifecyclePanel />);
  });
}

async function settle(): Promise<void> {
  await new Promise((resolve) => setTimeout(resolve, 0));
}

function buttonWithText(container: ParentNode, text: string): HTMLButtonElement {
  const button = [...container.querySelectorAll<HTMLButtonElement>('button')].find(
    (candidate) => candidate.textContent?.trim() === text,
  );
  assert.ok(button, `button ${text} should be present`);
  return button;
}

function installFetch(
  handler: (route: string, init?: RequestInit) => Promise<Response>,
): {routes: string[]; bodies: Record<string, unknown>[]} {
  const routes: string[] = [];
  const bodies: Record<string, unknown>[] = [];
  globalThis.fetch = (async (input, init) => {
    const url = new URL(String(input), 'http://localhost');
    const route = `${url.pathname}${url.search}`;
    routes.push(route);
    if (init?.body) bodies.push(JSON.parse(String(init.body)) as Record<string, unknown>);
    return handler(route, init);
  }) as typeof fetch;
  return {routes, bodies};
}

function jsonResponse(data: unknown): Response {
  return new Response(JSON.stringify({success: true, data}), {
    headers: {'Content-Type': 'application/json'},
  });
}

function deniedResponse(error: string): Response {
  return new Response(JSON.stringify({
    success: false,
    data: {state: 'packvm_lifecycle_denied', operation: 'consent'},
    error,
  }), {status: 409, headers: {'Content-Type': 'application/json'}});
}

let previousState: ReturnType<typeof useAppStore.getState>;
let originalFetch: typeof fetch;
let surface: {dom: JSDOM; container: HTMLElement; root: Root} | null = null;

beforeEach(() => {
  setRuntimeDispatchStatus('runtime_ready');
  previousState = useAppStore.getState();
  originalFetch = globalThis.fetch;
  surface = createSurface();
});

afterEach(async () => {
  if (surface) {
    await act(async () => surface?.root.unmount());
    surface.dom.window.close();
  }
  globalThis.fetch = originalFetch;
  useAppStore.setState(previousState, true);
  surface = null;
});

test('PackVM GUI completes prepare, consent, provision, doctor, and hides host paths', {concurrency: false}, async () => {
  const doctorControl = configureStore();
  const {routes, bodies} = installFetch(async (route, init) => {
    if (route === '/api/v4/packvm/prepare') return jsonResponse(plan);
    if (route === '/api/v4/packvm/consent') return jsonResponse(consent);
    if (route === '/api/v4/packvm/provision') {
      const body = JSON.parse(String(init?.body)) as {operation_id: string};
      doctorControl.setNextDoctor(healthyDoctor);
      return jsonResponse({...operation('succeeded'), operation_id: body.operation_id});
    }
    throw new Error(`unexpected route ${route}`);
  });
  assert.ok(surface);
  await renderPanel(surface.root);
  assert.ok(
    surface.container.querySelector('[data-packvm-error-icon="readiness-warning"]'),
  );
  assert.equal(
    surface.container.querySelector('[data-packvm-error-icon="readiness-warning"]')
      ?.classList.contains('lucide-triangle-alert'),
    true,
  );

  await act(async () => buttonWithText(surface.container, 'Prepare plan').click());
  await settle();
  assert.match(surface.container.textContent ?? '', /Pinned plan/);
  assert.match(surface.container.textContent ?? '', /Configuration digest/);
  assert.match(surface.container.textContent ?? '', /Guest runner digest/);
  assert.match(surface.container.textContent ?? '', /Required disk space/);
  assert.doesNotMatch(surface.container.textContent ?? '', /Users\/haru|limactl/);

  const checkbox = surface.container.querySelector<HTMLInputElement>('input[type="checkbox"]');
  assert.ok(checkbox);
  await act(async () => checkbox.click());
  await act(async () => buttonWithText(surface.container, 'Record explicit consent').click());
  await settle();
  assert.match(surface.container.textContent ?? '', /Plan consent recorded/);

  await act(async () => buttonWithText(surface.container, 'Provision PackVM').click());
  await settle();
  assert.match(surface.container.textContent ?? '', /Healthy and attested/);
  assert.match(surface.container.textContent ?? '', /Provisioned/);
  assert.deepEqual(routes, [
    '/api/v4/packvm/prepare',
    '/api/v4/packvm/consent',
    '/api/v4/packvm/provision',
  ]);
  assert.deepEqual(bodies[1], {
    plan_digest: plan.plan_digest,
    ceremony_nonce: plan.ceremony_nonce,
    confirmation: plan.confirmation,
    approve_image_download: true,
  });
  assert.equal(bodies[2].consent_id, consent.consent_id);
  assert.match(String(bodies[2].operation_id), /^[0-9a-f-]{36}$/i);
});

test('PackVM GUI displays an unavailable plan reason and keeps provisioning disabled', {concurrency: false}, async () => {
  configureStore();
  const unavailableReason = 'A Developer ID signed PackVM helper is required.';
  const unavailablePlan = {
    ...plan,
    launcher_reason: unavailableReason,
    runtime_path_status: 'unsafe',
    image_source: 'unavailable',
    image_digest: digest('0'),
    image_download_required: false,
    config_digest: digest('0'),
    guest_runner_digest: digest('0'),
    host_build_digest: digest('0'),
  };
  const {routes} = installFetch(async (route) => {
    if (route === '/api/v4/packvm/prepare') return jsonResponse(unavailablePlan);
    throw new Error(`unexpected route ${route}`);
  });
  assert.ok(surface);
  await renderPanel(surface.root);

  await act(async () => buttonWithText(surface.container, 'Prepare plan').click());
  await settle();

  assert.match(surface.container.textContent ?? '', new RegExp(unavailableReason));
  assert.match(surface.container.textContent ?? '', /Image sourceunavailable/);
  assert.equal(surface.container.querySelector('input[type="checkbox"]'), null);
  assert.equal(
    [...surface.container.querySelectorAll('button')].some(
      (button) => button.textContent?.trim() === 'Record explicit consent',
    ),
    false,
  );
  assert.equal(
    [...surface.container.querySelectorAll('button')].some(
      (button) => button.textContent?.trim() === 'Provision PackVM',
    ),
    false,
  );
  assert.deepEqual(routes, ['/api/v4/packvm/prepare']);
});

test('PackVM GUI surfaces consent denial and prevents provisioning fallback', {concurrency: false}, async () => {
  configureStore();
  const {routes} = installFetch(async (route) => {
    if (route === '/api/v4/packvm/prepare') return jsonResponse(plan);
    if (route === '/api/v4/packvm/consent') return deniedResponse('PackVM consent does not match a pending plan');
    throw new Error(`unexpected route ${route}`);
  });
  assert.ok(surface);
  await renderPanel(surface.root);
  await act(async () => buttonWithText(surface.container, 'Prepare plan').click());
  await settle();
  const checkbox = surface.container.querySelector<HTMLInputElement>('input[type="checkbox"]');
  assert.ok(checkbox);
  await act(async () => checkbox.click());
  await act(async () => buttonWithText(surface.container, 'Record explicit consent').click());
  await settle();
  assert.match(surface.container.textContent ?? '', /does not match a pending plan/);
  assert.equal(buttonWithText(surface.container, 'Record explicit consent').disabled, false);
  assert.equal(routes.includes('/api/v4/packvm/provision'), false);
});

test('PackVM GUI rejects tampered consent and stale operation responses', {concurrency: false}, async () => {
  configureStore();
  let tamperOperation = false;
  const {routes} = installFetch(async (route) => {
    if (route === '/api/v4/packvm/prepare') return jsonResponse(plan);
    if (route === '/api/v4/packvm/consent') return jsonResponse({...consent, plan_digest: digest('9')});
    if (route === '/api/v4/packvm/provision') {
      tamperOperation = true;
      return jsonResponse(operation('queued', {plan_digest: digest('9')}));
    }
    throw new Error(`unexpected route ${route}`);
  });
  assert.ok(surface);
  await renderPanel(surface.root);
  await act(async () => buttonWithText(surface.container, 'Prepare plan').click());
  await settle();
  const checkbox = surface.container.querySelector<HTMLInputElement>('input[type="checkbox"]');
  assert.ok(checkbox);
  await act(async () => checkbox.click());
  await act(async () => buttonWithText(surface.container, 'Record explicit consent').click());
  await settle();
  assert.match(surface.container.textContent ?? '', /different pinned plan/);
  assert.equal(buttonWithText(surface.container, 'Record explicit consent').disabled, false);
  assert.equal(tamperOperation, false);
  assert.equal(routes.includes('/api/v4/packvm/provision'), false);
});

test('PackVM GUI clears a timeout and keeps the ceremony retryable', {concurrency: false}, async () => {
  configureStore();
  installFetch(async (route) => {
    if (route === '/api/v4/packvm/prepare') {
      throw new Error('POST request timed out after 10000ms: /api/v4/packvm/prepare');
    }
    throw new Error(`unexpected route ${route}`);
  });
  assert.ok(surface);
  await renderPanel(surface.root);
  await act(async () => buttonWithText(surface.container, 'Prepare plan').click());
  await settle();
  assert.match(surface.container.textContent ?? '', /timed out after 10000ms/);
  assert.equal(buttonWithText(surface.container, 'Prepare plan').disabled, false);
});

test('PackVM GUI displays typed failure diagnostics from authoritative progress', {concurrency: false}, async () => {
  configureStore();
  let copied = '';
  assert.ok(surface);
  Object.defineProperty(surface.dom.window.navigator, 'clipboard', {
    configurable: true,
    value: {writeText: async (text: string) => { copied = text; }},
  });
  writeSafeStorageValue(getBrowserStorage('local'), 'tobkiri-launcher-packvm-operation', operationId);
  installFetch(async (route) => {
    assert.equal(route, `/api/v4/packvm/progress?operation_id=${operationId}`);
    return jsonResponse(operation('failed', {
      error_type: 'PackVMReconciliationRequired',
      diagnostic: {
        code: 'packvm_lima_process_failed',
        stage: 'doctor',
        kind: 'exit',
        exit_code: 23,
        stderr: 'catalog/profile digest mismatch',
      },
    }));
  });
  await renderPanel(surface.root);
  await settle();
  assert.match(surface.container.textContent ?? '', /PackVMReconciliationRequired/);
  assert.match(surface.container.textContent ?? '', /packvm_lima_process_failed/);
  assert.match(surface.container.textContent ?? '', /doctor/);
  assert.match(surface.container.textContent ?? '', /catalog\/profile digest mismatch/);
  assert.ok(surface.container.querySelector('[aria-label="Typed PackVM failure diagnostic"]'));
  const copy = surface.container.querySelector<HTMLButtonElement>(
    'button[aria-label="Copy typed PackVM failure diagnostic"]',
  );
  assert.ok(copy);
  await act(async () => {
    copy.click();
    await Promise.resolve();
  });
  assert.equal(copied, [
    'Failure type: PackVMReconciliationRequired',
    'Diagnostic code: packvm_lima_process_failed',
    'Stage: doctor',
    'Process result: exit (23)',
    'Host diagnostic: catalog/profile digest mismatch',
  ].join('\n'));
});

test('PackVM GUI coalesces rapid doctor refresh clicks', {concurrency: false}, async () => {
  const doctorControl = configureStore(healthyDoctor);
  assert.ok(surface);
  await renderPanel(surface.root);
  await settle();
  const initialCalls = doctorControl.refreshCalls();
  const button = buttonWithText(surface.container, 'Run doctor again');
  await act(async () => {
    button.click();
    button.click();
  });
  await settle();
  assert.equal(doctorControl.refreshCalls(), initialCalls + 1);
});

test('PackVM GUI cancels only a queued operation', {concurrency: false}, async () => {
  configureStore();
  const {routes} = installFetch(async (route, init) => {
    if (route === '/api/v4/packvm/prepare') return jsonResponse(plan);
    if (route === '/api/v4/packvm/consent') return jsonResponse(consent);
    if (route === '/api/v4/packvm/provision') {
      const body = JSON.parse(String(init?.body)) as {operation_id: string};
      return jsonResponse({...operation('queued'), operation_id: body.operation_id});
    }
    if (route === '/api/v4/packvm/cancel') {
      const body = JSON.parse(String(init?.body)) as {operation_id: string};
      return jsonResponse({...operation('cancelled'), operation_id: body.operation_id});
    }
    throw new Error(`unexpected route ${route}`);
  });
  assert.ok(surface);
  await renderPanel(surface.root);
  await act(async () => buttonWithText(surface.container, 'Prepare plan').click());
  await settle();
  const checkbox = surface.container.querySelector<HTMLInputElement>('input[type="checkbox"]');
  assert.ok(checkbox);
  await act(async () => checkbox.click());
  await act(async () => buttonWithText(surface.container, 'Record explicit consent').click());
  await settle();
  await act(async () => buttonWithText(surface.container, 'Provision PackVM').click());
  await settle();
  await act(async () => buttonWithText(surface.container, 'Cancel queued provisioning').click());
  await settle();
  assert.match(surface.container.textContent ?? '', /Cancelled/);
  assert.equal(routes.at(-1), '/api/v4/packvm/cancel');
});

test('PackVM GUI stops and cleans only the authenticated instance', {concurrency: false}, async () => {
  configureStore(notReadyDoctor);
  const {routes, bodies} = installFetch(async (route, init) => {
    if (route === '/api/v4/packvm/stop') {
      const body = JSON.parse(String(init?.body)) as {confirmation: string};
      assert.equal(body.confirmation, 'STOP tobkiri-packvm-v4');
      return jsonResponse(notReadyDoctor);
    }
    if (route === '/api/v4/packvm/cleanup') {
      const body = JSON.parse(String(init?.body)) as {
        confirmation: string;
        operation_id: string;
        source_operation_id: string | null;
      };
      assert.equal(body.confirmation, 'DELETE tobkiri-packvm-v4');
      return jsonResponse({
        operation_id: body.operation_id,
        operation_kind: 'cleanup',
        state: 'succeeded',
        plan_digest: digest('0'),
        updated_unix: 1,
        result: {
          ready: false,
          instance: 'tobkiri-packvm-v4',
          cleanup_confirmation: 'DELETE tobkiri-packvm-v4',
          missing: false,
        },
      });
    }
    throw new Error(`unexpected route ${route}`);
  });
  assert.ok(surface);
  await renderPanel(surface.root);
  await act(async () => buttonWithText(surface.container, 'Stop PackVM').click());
  await settle();
  await act(async () => buttonWithText(surface.container, 'Clean up PackVM').click());
  const cleanupInput = surface.container.querySelector<HTMLInputElement>('#input-cleanup-confirmation');
  assert.ok(cleanupInput);
  await act(async () => {
    const setter = Object.getOwnPropertyDescriptor(
      window.HTMLInputElement.prototype,
      'value',
    )?.set;
    setter?.call(cleanupInput, 'DELETE tobkiri-packvm-v4');
    cleanupInput.dispatchEvent(new window.InputEvent('input', {
      bubbles: true,
      inputType: 'insertText',
      data: 'DELETE tobkiri-packvm-v4',
    }));
    cleanupInput.dispatchEvent(new window.Event('change', {bubbles: true}));
  });
  await settle();
  assert.equal(buttonWithText(surface.container, 'Delete authenticated PackVM').disabled, false);
  await act(async () => buttonWithText(surface.container, 'Delete authenticated PackVM').click());
  await settle();
  assert.equal(routes.at(-1), '/api/v4/packvm/cleanup');
  assert.deepEqual(bodies[0], {confirmation: 'STOP tobkiri-packvm-v4'});
  assert.equal(bodies[1]?.confirmation, 'DELETE tobkiri-packvm-v4');
  assert.equal(bodies[1]?.source_operation_id, null);
  assert.match(String(bodies[1]?.operation_id), /^[0-9a-f-]{36}$/i);
  assert.doesNotMatch(surface.container.textContent ?? '', /Confirm PackVM cleanup/);
  assert.match(surface.container.textContent ?? '', /PackVM instance was cleaned up/);
});

test('PackVM GUI presents diagnostic severity, owner, and contribution evidence', {concurrency: false}, async () => {
  configureStore(healthyDoctor);
  useAppStore.setState({
    frontendCatalog: {
      version: 'rumi.ui.contribution.v1',
      profile_id: 'profile-a',
      profile_revision: digest('1'),
      activation_id: 'activation:profile-a',
      plan_hash: digest('2'),
      contributions: [],
      diagnostics: [{
        code: 'production_backend_unavailable',
        severity: 'error',
        message: 'The verified backend is not available.',
        owner_pack_id: 'research-pack',
        contribution_id: 'research.contribution',
        operation_id: 'research.operation',
      }],
      quarantined_pack_ids: [],
      catalog_hash: digest('3'),
    },
  });
  assert.ok(surface);
  await renderPanel(surface.root);
  assert.match(surface.container.textContent ?? '', /production_backend_unavailable/);
  assert.match(surface.container.textContent ?? '', /research-pack/);
  assert.match(surface.container.textContent ?? '', /research\.contribution/);
  assert.match(surface.container.textContent ?? '', /Invocation remains unavailable/);
  assert.ok(surface.container.querySelector('[role="alert"]'));
});

test('PackVM GUI resumes a persisted interrupted operation after restart', {concurrency: false}, async () => {
  const doctorControl = configureStore();
  writeSafeStorageValue(getBrowserStorage('local'), 'tobkiri-launcher-packvm-operation', operationId);
  let progressReads = 0;
  installFetch(async (route) => {
    if (route === `/api/v4/packvm/progress?operation_id=${operationId}`) {
      progressReads += 1;
      if (progressReads === 1) return jsonResponse(operation('interrupted'));
      doctorControl.setNextDoctor(healthyDoctor);
      return jsonResponse(operation('succeeded'));
    }
    throw new Error(`unexpected route ${route}`);
  });
  assert.ok(surface);
  await renderPanel(surface.root);
  await settle();
  assert.match(surface.container.textContent ?? '', /Interrupted — restart detected/);
  await act(async () => buttonWithText(surface.container, 'Resume status').click());
  await settle();
  assert.match(surface.container.textContent ?? '', /Provisioned/);
  assert.match(surface.container.textContent ?? '', /Healthy and attested/);
  assert.equal(progressReads, 2);
});

test('PackVM GUI clears a tampered durable operation id after server validation rejects it', {concurrency: false}, async () => {
  configureStore();
  const tamperedId = '22222222-2222-4222-8222-222222222222';
  writeSafeStorageValue(getBrowserStorage('local'), 'tobkiri-launcher-packvm-operation', tamperedId);
  installFetch(async (route) => {
    assert.equal(route, `/api/v4/packvm/progress?operation_id=${tamperedId}`);
    return new Response(JSON.stringify({
      success: false,
      data: null,
      error: 'packvm_operation_unknown',
    }), {status: 404, headers: {'Content-Type': 'application/json'}});
  });
  assert.ok(surface);
  await renderPanel(surface.root);
  await settle();
  assert.equal(readSafeStorageValue(getBrowserStorage('local'), 'tobkiri-launcher-packvm-operation'), null);
  assert.match(surface.container.textContent ?? '', /could not be resumed|packvm_operation_unknown/i);
});
