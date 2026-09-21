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

test('an explicit restart read budget is not cut short by the default transport timeout', async (context) => {
  context.mock.timers.enable({apis: ['setTimeout']});
  const coordinator = new GetRequestCoordinator({hardTimeoutMs: 10});
  const pending = request(coordinator, '/setup', 'foreground', (signal) => new Promise<string>((resolve, reject) => {
    signal.addEventListener('abort', () => reject(signal.reason), {once: true});
    setTimeout(() => resolve('active'), 20);
  }), 60);
  await Promise.resolve();
  context.mock.timers.tick(20);
  assert.equal(await pending, 'active');
});

test('a longer recovery read can join an existing short read while retaining a finite deadline', async (context) => {
  context.mock.timers.enable({apis: ['setTimeout', 'Date'], now: 0});
  for (const completes of [true, false]) {
    const coordinator = new GetRequestCoordinator({hardTimeoutMs: 10});
    const factory = (signal: AbortSignal) => new Promise<string>((resolve, reject) => {
      signal.addEventListener('abort', () => reject(signal.reason), {once: true});
      if (completes) setTimeout(() => resolve('active'), 40);
    });
    const first = request(coordinator, '/setup', 'prefetch', factory, 5);
    const firstTimeout = assert.rejects(first, RequestTimeoutError);
    await Promise.resolve();
    context.mock.timers.tick(6);
    await firstTimeout;
    const recovery = request(coordinator, '/setup', 'foreground', factory, 60);
    if (completes) {
      context.mock.timers.tick(34);
      assert.equal(await recovery, 'active');
    } else {
      const bounded = assert.rejects(recovery, /Shared GET request exceeded 60ms/);
      context.mock.timers.tick(54);
      await bounded;
    }
  }
});

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
