import assert from 'node:assert/strict';
import {afterEach, beforeEach, test} from 'node:test';

import type {ApiPackVMDoctor} from './lib/apiTypes';
import {setRuntimeDispatchStatus} from './lib/runtimeDispatchGate';
import {useAppStore} from './store';

const readyDoctor: ApiPackVMDoctor = {
  ready: true,
  backend_id: 'tobkiri.python-pack-v4',
  platform: 'macos',
  instance: 'tobkiri-packvm-v4',
  reason: null,
  attestation_digest: 'sha256:ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff',
};

type Deferred<T> = {
  promise: Promise<T>;
  resolve: (value: T) => void;
};

function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((nextResolve) => {
    resolve = nextResolve;
  });
  return {promise, resolve};
}

function jsonResponse(data: unknown): Response {
  return new Response(JSON.stringify({success: true, data}), {
    headers: {'Content-Type': 'application/json'},
  });
}

async function flush(): Promise<void> {
  await new Promise<void>((resolve) => setTimeout(resolve, 0));
}

function projectionCounter(): {
  loadPacks: () => Promise<void>;
  loadFrontendCatalog: () => Promise<void>;
  counts: {packs: number; catalog: number};
  release: () => void;
} {
  const packGate = deferred<void>();
  const catalogGate = deferred<void>();
  let packFlight: Promise<void> | null = null;
  let catalogFlight: Promise<void> | null = null;
  const counts = {packs: 0, catalog: 0};
  return {
    loadPacks: () => {
      if (!packFlight) {
        counts.packs += 1;
        packFlight = packGate.promise.finally(() => { packFlight = null; });
      }
      return packFlight;
    },
    loadFrontendCatalog: () => {
      if (!catalogFlight) {
        counts.catalog += 1;
        catalogFlight = catalogGate.promise.finally(() => { catalogFlight = null; });
      }
      return catalogFlight;
    },
    counts,
    release: () => {
      packGate.resolve();
      catalogGate.resolve();
    },
  };
}

let previousState: ReturnType<typeof useAppStore.getState>;
let originalFetch: typeof fetch;
let previousWindow: Window | undefined;

beforeEach(() => {
  previousState = useAppStore.getState();
  originalFetch = globalThis.fetch;
  previousWindow = globalThis.window;
  Object.defineProperty(globalThis, 'window', {
    value: {location: {href: 'http://localhost/panel/setup'}},
    configurable: true,
  });
  setRuntimeDispatchStatus('runtime_ready');
});

afterEach(() => {
  globalThis.fetch = originalFetch;
  useAppStore.setState(previousState, true);
  setRuntimeDispatchStatus('unknown');
  if (previousWindow) {
    Object.defineProperty(globalThis, 'window', {
      value: previousWindow,
      configurable: true,
    });
  } else {
    Reflect.deleteProperty(globalThis, 'window');
  }
});

function installDoctorResponse(): {gate: Deferred<Response>; requests: number[]} {
  const gate = deferred<Response>();
  const requests = [0];
  globalThis.fetch = (async (input) => {
    const url = new URL(String(input), 'http://localhost');
    assert.equal(url.pathname, '/api/v4/packvm/doctor');
    requests[0] += 1;
    return gate.promise;
  }) as typeof fetch;
  return {gate, requests};
}

function configureProjections(counter: ReturnType<typeof projectionCounter>): void {
  useAppStore.setState({
    packVmDoctor: null,
    packVmDoctorLoading: false,
    packVmError: null,
    frontendCatalog: null,
    frontendCatalogError: null,
    loadPacks: counter.loadPacks,
    loadFrontendCatalog: counter.loadFrontendCatalog,
  });
}

test('same-mode doctor refreshes share one flight', {concurrency: false}, async () => {
  const counter = projectionCounter();
  configureProjections(counter);
  const doctor = installDoctorResponse();

  const first = useAppStore.getState().refreshPackVMDoctor({reconcile: false});
  const second = useAppStore.getState().refreshPackVMDoctor({reconcile: false});
  assert.strictEqual(first, second);
  await flush();
  assert.equal(doctor.requests[0], 1);
  doctor.gate.resolve(jsonResponse(readyDoctor));
  await Promise.all([first, second]);
  assert.deepEqual(counter.counts, {packs: 0, catalog: 0});
});

test('same reconcile-mode refreshes share doctor and projection work', {concurrency: false}, async () => {
  const counter = projectionCounter();
  configureProjections(counter);
  const doctor = installDoctorResponse();

  const first = useAppStore.getState().refreshPackVMDoctor();
  const second = useAppStore.getState().refreshPackVMDoctor({reconcile: true});
  assert.strictEqual(first, second);
  await flush();
  doctor.gate.resolve(jsonResponse(readyDoctor));
  await flush();
  assert.deepEqual(counter.counts, {packs: 1, catalog: 1});
  counter.release();
  await Promise.all([first, second]);
});

async function assertOppositeOrdering(
  firstOptions: {reconcile?: boolean},
  secondOptions: {reconcile?: boolean},
): Promise<void> {
  const counter = projectionCounter();
  configureProjections(counter);
  const doctor = installDoctorResponse();
  const first = useAppStore.getState().refreshPackVMDoctor(firstOptions);
  const second = useAppStore.getState().refreshPackVMDoctor(secondOptions);
  assert.notStrictEqual(first, second);
  await flush();
  assert.equal(doctor.requests[0], 1);
  doctor.gate.resolve(jsonResponse(readyDoctor));

  const recovery = firstOptions.reconcile === false ? first : second;
  await recovery;
  await flush();

  // Setup's explicit projection sequence must join any normal panel refresh
  // already in flight instead of forcing a second request.
  const setupPacks = useAppStore.getState().loadPacks(false, {
    skipMutationReconciliation: true,
  });
  const setupCatalog = useAppStore.getState().loadFrontendCatalog(false);
  assert.deepEqual(counter.counts, {packs: 1, catalog: 1});

  counter.release();
  await Promise.all([first, second, setupPacks, setupCatalog]);
}

test('observe-first recovery and panel refresh do not duplicate projections', {concurrency: false}, async () => {
  await assertOppositeOrdering(
    {reconcile: false},
    {reconcile: true},
  );
});

test('panel-first refresh is superseded semantically by recovery without duplicate projections', {concurrency: false}, async () => {
  await assertOppositeOrdering(
    {reconcile: true},
    {reconcile: false},
  );
});
