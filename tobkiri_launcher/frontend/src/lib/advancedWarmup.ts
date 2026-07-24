import {getApiRequestCacheSnapshot, prefetchApiGet} from './api';
import type {
  CapabilityGraphsResponseData,
  CapabilityProfilesResponseData,
} from './apiTypes';
import {advancedRouteModuleSources} from './routeModules';
import {collectManifestAssets, type ViteManifest} from './routeManifest';

const WARMUP_DISABLE_KEY = 'tobkiri:disable-advanced-warmup';
const PREFETCH_TIMEOUT_MS = 2_500;
const PREFETCH_BATCH_SIZE = 2;
const PREFETCH_IDLE_TIMEOUT_MS = 1_500;

type WarmupPhase = 'disabled' | 'idle' | 'scheduled' | 'running' | 'complete';
type IdleCapableWindow = Window & {
  __TOBKIRI_WARMUP__?: AdvancedWarmupSnapshot;
  requestIdleCallback?: (
    callback: (deadline: {didTimeout: boolean; timeRemaining: () => number}) => void,
    options?: {timeout: number},
  ) => number;
  cancelIdleCallback?: (handle: number) => void;
};

export interface AdvancedWarmupSnapshot {
  cache: ReturnType<typeof getApiRequestCacheSnapshot>;
  completedAt: number | null;
  dataFailures: Array<{path: string; message: string}>;
  homeCommittedAt: number | null;
  memoryAfter: number | null;
  memoryBefore: number | null;
  phase: WarmupPhase;
  prefetchedScripts: number;
  prefetchedStyles: number;
  startedAt: number | null;
}

const insertedPrefetchKeys = new Set<string>();
const snapshot: AdvancedWarmupSnapshot = {
  cache: getApiRequestCacheSnapshot(),
  completedAt: null,
  dataFailures: [],
  homeCommittedAt: null,
  memoryAfter: null,
  memoryBefore: null,
  phase: 'idle',
  prefetchedScripts: 0,
  prefetchedStyles: 0,
  startedAt: null,
};

let scheduled = false;
let started = false;

function browserWindow(): IdleCapableWindow {
  return window as IdleCapableWindow;
}

function publishSnapshot(): void {
  snapshot.cache = getApiRequestCacheSnapshot();
  if (typeof window !== 'undefined') browserWindow().__TOBKIRI_WARMUP__ = {...snapshot};
}

function currentHeapSize(): number | null {
  const memory = (performance as Performance & {
    memory?: {usedJSHeapSize?: number};
  }).memory;
  return typeof memory?.usedJSHeapSize === 'number' ? memory.usedJSHeapSize : null;
}

function warmupDisabled(): boolean {
  if (import.meta.env.VITE_DISABLE_ADVANCED_WARMUP === '1') return true;
  try {
    return window.localStorage.getItem(WARMUP_DISABLE_KEY) === '1';
  } catch {
    return false;
  }
}

function scheduleIdle(callback: () => void): () => void {
  const target = browserWindow();
  if (typeof target.requestIdleCallback === 'function') {
    const handle = target.requestIdleCallback(callback, {timeout: PREFETCH_IDLE_TIMEOUT_MS});
    return () => target.cancelIdleCallback?.(handle);
  }
  const handle = window.setTimeout(callback, 180);
  return () => window.clearTimeout(handle);
}

function assetUrl(file: string): string {
  return new URL(file, document.baseURI).toString();
}

function appendPrefetchLink(rel: 'prefetch' | 'preload', file: string): boolean {
  const href = assetUrl(file);
  const key = `${rel}:${href}`;
  if (insertedPrefetchKeys.has(key)) return false;
  const alreadyPresent = [...document.head.querySelectorAll<HTMLLinkElement>(`link[rel="${rel}"]`)]
    .some((link) => link.href === href);
  insertedPrefetchKeys.add(key);
  if (alreadyPresent) return false;

  const link = document.createElement('link');
  link.rel = rel;
  link.href = href;
  link.setAttribute('fetchpriority', 'low');
  link.as = file.endsWith('.css') ? 'style' : 'script';
  document.head.append(link);
  return true;
}

async function fetchBuildManifest(): Promise<ViteManifest> {
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), PREFETCH_TIMEOUT_MS);
  try {
    const response = await fetch(new URL('manifest.json', document.baseURI), {
      cache: 'force-cache',
      credentials: 'same-origin',
      signal: controller.signal,
    });
    if (!response.ok) throw new Error(`manifest HTTP ${response.status}`);
    return await response.json() as ViteManifest;
  } finally {
    window.clearTimeout(timer);
  }
}

async function prefetchAdvancedCode(): Promise<void> {
  const manifest = await fetchBuildManifest();
  const assets = collectManifestAssets(
    manifest,
    Object.values(advancedRouteModuleSources),
  );

  for (const style of assets.styles) {
    if (appendPrefetchLink('prefetch', style)) snapshot.prefetchedStyles += 1;
  }

  for (let index = 0; index < assets.scripts.length; index += PREFETCH_BATCH_SIZE) {
    if (document.visibilityState === 'hidden') {
      await new Promise<void>((resolve) => {
        const onVisibility = () => {
          if (document.visibilityState !== 'visible') return;
          document.removeEventListener('visibilitychange', onVisibility);
          resolve();
        };
        document.addEventListener('visibilitychange', onVisibility);
      });
    }
    const batch = assets.scripts.slice(index, index + PREFETCH_BATCH_SIZE);
    for (const script of batch) {
      // `prefetch` populates the HTTP cache without evaluating every Advanced
      // module and pinning CodeMirror/ReactFlow in the JavaScript heap.
      if (appendPrefetchLink('prefetch', script)) snapshot.prefetchedScripts += 1;
    }
    publishSnapshot();
    await new Promise<void>((resolve) => scheduleIdle(resolve));
  }
}

function recordFailure(path: string, error: unknown): void {
  const message = error instanceof Error ? error.message : String(error);
  snapshot.dataFailures.push({path, message});
  console.warn(`[advanced-warmup] ${path}: ${message}`);
  publishSnapshot();
}

async function prefetch(path: string): Promise<unknown> {
  try {
    return await prefetchApiGet(path, {timeoutMs: PREFETCH_TIMEOUT_MS});
  } catch (error) {
    recordFailure(path, error);
    return null;
  }
}

async function prefetchAdvancedData(): Promise<void> {
  // Keep this list to lightweight route-level collections. Home owns dashboard
  // and startup-profile requests; API Map and update checks can be large or
  // externally network-bound and are intentionally left to explicit intent.
  const firstWave = [
    '/api/panel/packs',
    '/api/panel/flows',
    '/api/panel/settings/profile',
    '/api/panel/version',
  ];

  for (let index = 0; index < firstWave.length; index += 2) {
    await Promise.all(firstWave.slice(index, index + 2).map(prefetch));
  }

  const [profiles, graphs] = await Promise.all([
    prefetchApiGet<CapabilityProfilesResponseData>('/api/panel/profiles', {
      timeoutMs: PREFETCH_TIMEOUT_MS,
    }).catch((error) => {
      recordFailure('/api/panel/profiles', error);
      return null;
    }),
    prefetchApiGet<CapabilityGraphsResponseData>('/api/panel/graphs', {
      timeoutMs: PREFETCH_TIMEOUT_MS,
    }).catch((error) => {
      recordFailure('/api/panel/graphs', error);
      return null;
    }),
  ]);

  const firstProfileId = profiles?.profiles[0]?.profile_id;
  if (firstProfileId) {
    await prefetch(`/api/panel/profiles/${encodeURIComponent(firstProfileId)}/nodes`);
  }

  // Keep response references out of long-lived warmup state. The API cache is
  // one-shot, TTL-limited, entry-limited, and rejects oversized payloads.
  void graphs;
}

async function runWarmup(): Promise<void> {
  if (started) return;
  started = true;
  snapshot.phase = 'running';
  snapshot.startedAt = performance.now();
  snapshot.memoryBefore = currentHeapSize();
  publishSnapshot();
  performance.mark('tobkiri:advanced-warmup-start');

  const [codeResult, dataResult] = await Promise.allSettled([
    prefetchAdvancedCode(),
    prefetchAdvancedData(),
  ]);
  if (codeResult.status === 'rejected') recordFailure('advanced-code', codeResult.reason);
  if (dataResult.status === 'rejected') recordFailure('advanced-data', dataResult.reason);

  snapshot.phase = 'complete';
  snapshot.completedAt = performance.now();
  snapshot.memoryAfter = currentHeapSize();
  publishSnapshot();
  performance.mark('tobkiri:advanced-warmup-complete');
  performance.measure(
    'tobkiri:advanced-warmup',
    'tobkiri:advanced-warmup-start',
    'tobkiri:advanced-warmup-complete',
  );
}

export function scheduleAdvancedWarmup(homeCommittedAt: number): () => void {
  if (snapshot.homeCommittedAt === null) snapshot.homeCommittedAt = homeCommittedAt;
  if (warmupDisabled()) {
    snapshot.phase = 'disabled';
    publishSnapshot();
    return () => undefined;
  }
  if (scheduled || started) return () => undefined;
  scheduled = true;
  snapshot.phase = 'scheduled';
  publishSnapshot();

  let cancelIdle = () => undefined;
  let cancelled = false;
  const firstFrame = window.requestAnimationFrame(() => {
    const secondFrame = window.requestAnimationFrame(() => {
      cancelIdle = scheduleIdle(() => {
        if (!cancelled) void runWarmup();
      });
    });
    cancelIdle = () => window.cancelAnimationFrame(secondFrame);
  });

  return () => {
    cancelled = true;
    window.cancelAnimationFrame(firstFrame);
    cancelIdle();
    if (!started) {
      scheduled = false;
      snapshot.phase = 'idle';
      publishSnapshot();
    }
  };
}
