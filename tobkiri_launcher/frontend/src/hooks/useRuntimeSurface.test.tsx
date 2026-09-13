import assert from 'node:assert/strict';
import {act} from 'react';
import {createRoot, type Root} from 'react-dom/client';
import {JSDOM} from 'jsdom';
import test from 'node:test';

import {
  broadcastRuntimeSurfaceRefresh,
  useRuntimeSurface,
  type RuntimeSurfaceClient,
} from './useRuntimeSurface';
import {
  RUNTIME_SURFACE_API_VERSION,
  RuntimeSurfaceError,
  type RuntimeSurfaceEnvelope,
  type RuntimeSurfaceId,
} from '@/src/lib/runtimeSurface';

const digest = (character: string): string => `sha256:${character.repeat(64)}`;

function envelope(surface: RuntimeSurfaceId): RuntimeSurfaceEnvelope<unknown> {
  return {
    runtime_surface_api_version: RUNTIME_SURFACE_API_VERSION,
    surface,
    state: 'ready',
    profile_id: 'defaults',
    profile_revision: digest('a'),
    plan_digest: digest('b'),
    catalog_revision: digest('c'),
    records: {
      profile_lock: {digest: digest('d'), source_ref: 'profile-lock-v4://defaults/lock'},
      resolved_plan: {digest: digest('b'), source_ref: 'resolved-plan-v1://defaults/plan'},
      activation_record: {digest: digest('1'), source_ref: 'activation-record-v1://defaults/activation'},
      authority_snapshot: {digest: digest('e'), source_ref: 'authority-snapshot-v4://defaults/snapshot'},
    },
    data: {},
  };
}

function createSurface(): {dom: JSDOM; container: HTMLElement; root: Root} {
  const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>');
  Object.defineProperties(globalThis, {
    window: {value: dom.window, configurable: true},
    document: {value: dom.window.document, configurable: true},
    navigator: {value: dom.window.navigator, configurable: true},
  });
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  const container = dom.window.document.querySelector<HTMLElement>('#root');
  assert.ok(container);
  return {dom, container, root: createRoot(container)};
}

function SurfaceProbe({surface, client}: {surface: RuntimeSurfaceId; client: RuntimeSurfaceClient}) {
  const state = useRuntimeSurface(surface, client);
  return (
    <span
      data-surface={surface}
      data-status={state.status}
      data-stale={String(state.stale)}
      data-can-mutate={String(state.canMutate)}
      data-profile-revision={state.data?.profile_revision ?? 'none'}
    >
      {state.status}
    </span>
  );
}

test('activation refresh broadcast re-reads every mounted runtime surface', async () => {
  const previousWindow = globalThis.window;
  const previousDocument = globalThis.document;
  const {dom, container, root} = createSurface();
  const reads = {profile: 0, operations: 0};
  const profileClient: RuntimeSurfaceClient = {read: async <T,>() => { reads.profile += 1; return envelope('profile') as RuntimeSurfaceEnvelope<T>; }};
  const operationsClient: RuntimeSurfaceClient = {read: async <T,>() => { reads.operations += 1; return envelope('operations') as RuntimeSurfaceEnvelope<T>; }};
  try {
    await act(async () => {
      root.render(
        <>
          <SurfaceProbe surface="profile" client={profileClient} />
          <SurfaceProbe surface="operations" client={operationsClient} />
        </>,
      );
    });
    assert.deepEqual(reads, {profile: 1, operations: 1});
    await act(async () => { broadcastRuntimeSurfaceRefresh(); });
    assert.deepEqual(reads, {profile: 2, operations: 2});
  } finally {
    act(() => root.unmount());
    dom.window.close();
    Object.defineProperty(globalThis, 'window', {value: previousWindow, configurable: true});
    Object.defineProperty(globalThis, 'document', {value: previousDocument, configurable: true});
  }
});

test('stale refresh keeps the accepted envelope read-only and the next retry reads authoritatively', async () => {
  const previousWindow = globalThis.window;
  const previousDocument = globalThis.document;
  const {dom, container, root} = createSurface();
  const calls: Array<unknown> = [];
  let refresh!: (force?: boolean) => Promise<void>;
  const accepted = envelope('profile');
  const refreshed = {...accepted, profile_revision: digest('e')};
  const client: RuntimeSurfaceClient = {
    read: async <T,>(_surface, input): Promise<RuntimeSurfaceEnvelope<T>> => {
      calls.push(input);
      if (calls.length === 1) return accepted as RuntimeSurfaceEnvelope<T>;
      if (calls.length === 2) throw new RuntimeSurfaceError('STALE', 'stale guard');
      return refreshed as RuntimeSurfaceEnvelope<T>;
    },
  };

  function RetryProbe() {
    const state = useRuntimeSurface('profile', client);
    refresh = state.refresh;
    return (
      <span
        data-status={state.status}
        data-stale={String(state.stale)}
        data-can-mutate={String(state.canMutate)}
        data-profile-revision={state.data?.profile_revision ?? 'none'}
      />
    );
  }

  try {
    await act(async () => {
      root.render(<RetryProbe />);
    });
    assert.equal(container.firstElementChild?.getAttribute('data-status'), 'ready');
    assert.deepEqual(calls, [undefined]);

    await act(async () => { await refresh(); });
    assert.deepEqual(calls, [
      undefined,
      {
        expected_profile_revision: accepted.profile_revision,
        expected_plan_digest: accepted.plan_digest,
      },
    ]);
    assert.equal(container.firstElementChild?.getAttribute('data-status'), 'stale');
    assert.equal(container.firstElementChild?.getAttribute('data-stale'), 'true');
    assert.equal(container.firstElementChild?.getAttribute('data-can-mutate'), 'false');
    assert.equal(container.firstElementChild?.getAttribute('data-profile-revision'), accepted.profile_revision);

    await act(async () => { await refresh(); });
    assert.deepEqual(calls, [
      undefined,
      {
        expected_profile_revision: accepted.profile_revision,
        expected_plan_digest: accepted.plan_digest,
      },
      undefined,
    ]);
    assert.equal(container.firstElementChild?.getAttribute('data-status'), 'ready');
    assert.equal(container.firstElementChild?.getAttribute('data-stale'), 'false');
    assert.equal(container.firstElementChild?.getAttribute('data-profile-revision'), refreshed.profile_revision);
  } finally {
    act(() => root.unmount());
    dom.window.close();
    Object.defineProperty(globalThis, 'window', {value: previousWindow, configurable: true});
    Object.defineProperty(globalThis, 'document', {value: previousDocument, configurable: true});
  }
});
