import assert from 'node:assert/strict';
import test from 'node:test';

import {GetRequestCoordinator, RequestInvalidatedError, RequestTimeoutError} from './getRequestCoordinator.ts';

const request = <T>(
  coordinator: GetRequestCoordinator,
  key: string,
  mode: 'foreground' | 'prefetch',
  factory: (signal: AbortSignal) => Promise<T>,
  timeoutMs = 1_000,
) => coordinator.request({factory, key, mode, timeoutMs});

test('prefetch is consumed once by the first foreground request', async () => {
  const coordinator = new GetRequestCoordinator();
  let count = 0;
  const factory = async () => ({count: ++count});

  assert.deepEqual(await request(coordinator, '/packs', 'prefetch', factory), {count: 1});
  assert.equal(coordinator.snapshot().cacheEntries, 1);
  assert.deepEqual(await request(coordinator, '/packs', 'foreground', factory), {count: 1});
  assert.equal(coordinator.snapshot().cacheEntries, 0);
  assert.deepEqual(await request(coordinator, '/packs', 'foreground', factory), {count: 2});
});

test('foreground joining an in-flight prefetch prevents a redundant retained cache entry', async () => {
  const coordinator = new GetRequestCoordinator();
  let resolve!: (value: {ok: boolean}) => void;
  let count = 0;
  const pending = new Promise<{ok: boolean}>((next) => { resolve = next; });
  const factory = async () => { count += 1; return pending; };

  const warmup = request(coordinator, '/flows', 'prefetch', factory);
  const foreground = request(coordinator, '/flows', 'foreground', factory);
  resolve({ok: true});
  assert.deepEqual(await Promise.all([warmup, foreground]), [{ok: true}, {ok: true}]);
  assert.equal(count, 1);
  assert.equal(coordinator.snapshot().cacheEntries, 0);
});

test('invalidate aborts stale prefetch and prevents it from repopulating the cache', async () => {
  const coordinator = new GetRequestCoordinator();
  const pending = request(
    coordinator,
    '/graphs',
    'prefetch',
    (signal) => new Promise((_resolve, reject) => {
      signal.addEventListener('abort', () => reject(signal.reason), {once: true});
    }),
  );

  coordinator.invalidate();
  await assert.rejects(pending, /invalidated/);
  assert.equal(coordinator.snapshot().cacheEntries, 0);
});


test('foreground response invalidated by a mutation is rejected instead of publishing stale data', async () => {
  const coordinator = new GetRequestCoordinator();
  let resolve!: (value: string) => void;
  const pendingValue = new Promise<string>((next) => { resolve = next; });
  const pending = request(coordinator, '/packs', 'foreground', async () => pendingValue);

  coordinator.invalidate();
  resolve('stale');
  await assert.rejects(pending, RequestInvalidatedError);
});

test('session invalidation preserves only the request that performed the exchange', async () => {
  const coordinator = new GetRequestCoordinator();
  let resolveCurrent!: (value: string) => void;
  let resolveStale!: (value: string) => void;
  let currentSignal!: AbortSignal;
  const current = request(coordinator, '/current', 'foreground', async (signal) => {
    currentSignal = signal;
    return new Promise<string>((resolve) => { resolveCurrent = resolve; });
  });
  const stale = request(coordinator, '/stale', 'foreground', async () => (
    new Promise<string>((resolve) => { resolveStale = resolve; })
  ));

  await new Promise<void>((resolve) => queueMicrotask(resolve));
  coordinator.invalidate({preserveSignal: currentSignal});
  resolveCurrent('refreshed-session');
  resolveStale('old-session');

  assert.equal(await current, 'refreshed-session');
  await assert.rejects(stale, RequestInvalidatedError);
});

test('consumer timeout does not cancel a shared request that a foreground consumer can join', async () => {
  const coordinator = new GetRequestCoordinator({hardTimeoutMs: 5_000});
  let resolve!: (value: string) => void;
  const pending = new Promise<string>((next) => { resolve = next; });
  const factory = async () => pending;

  await assert.rejects(
    request(coordinator, '/profile', 'prefetch', factory, 5),
    RequestTimeoutError,
  );
  const foreground = request(coordinator, '/profile', 'foreground', factory, 1_000);
  resolve('ready');
  assert.equal(await foreground, 'ready');
});
