export type GetRequestMode = 'foreground' | 'prefetch';

export interface GetRequestCoordinatorOptions {
  cacheTtlMs?: number;
  hardTimeoutMs?: number;
  maxCacheBytes?: number;
  maxEntries?: number;
  maxEntryBytes?: number;
}

export interface GetRequestSnapshot {
  cacheBytes: number;
  cacheEntries: number;
  epoch: number;
  foregroundInFlight: number;
  inFlight: number;
  prefetchInFlight: number;
}

export interface GetRequestInvalidationOptions {
  /**
   * The request currently refreshing an expired session. It has not yet read
   * the protected resource, so it can safely continue after the new session
   * is installed while every other in-flight request is made stale.
   */
  preserveSignal?: AbortSignal;
}

interface CacheEntry {
  bytes: number;
  expiresAt: number;
  value: unknown;
}

interface InFlightEntry {
  abortController: AbortController;
  epoch: number;
  foreground: boolean;
  promise: Promise<unknown>;
  extendDeadline: (timeoutMs: number) => void;
}

export class RequestInvalidatedError extends Error {
  constructor(key: string) {
    super(`GET request invalidated by a mutation or session change: ${key}`);
    this.name = 'RequestInvalidatedError';
  }
}

export class RequestTimeoutError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'RequestTimeoutError';
  }
}

function estimateBytes(value: unknown): number {
  try {
    const json = JSON.stringify(value);
    if (typeof TextEncoder !== 'undefined') return new TextEncoder().encode(json).byteLength;
    return json.length * 2;
  } catch {
    return Number.POSITIVE_INFINITY;
  }
}

function withConsumerTimeout<T>(promise: Promise<T>, timeoutMs: number, key: string): Promise<T> {
  if (!Number.isFinite(timeoutMs) || timeoutMs <= 0) return promise;
  return new Promise<T>((resolve, reject) => {
    const timer = globalThis.setTimeout(() => {
      reject(new RequestTimeoutError(`GET request timed out after ${timeoutMs}ms: ${key}`));
    }, timeoutMs);
    promise.then(resolve, reject).finally(() => globalThis.clearTimeout(timer));
  });
}

export class GetRequestCoordinator {
  private readonly cache = new Map<string, CacheEntry>();
  private cacheBytes = 0;
  private epoch = 0;
  private readonly inFlight = new Map<string, InFlightEntry>();
  private readonly cacheTtlMs: number;
  private readonly hardTimeoutMs: number;
  private readonly maxCacheBytes: number;
  private readonly maxEntries: number;
  private readonly maxEntryBytes: number;

  constructor(options: GetRequestCoordinatorOptions = {}) {
    this.cacheTtlMs = options.cacheTtlMs ?? 30_000;
    this.hardTimeoutMs = options.hardTimeoutMs ?? 30_000;
    this.maxCacheBytes = options.maxCacheBytes ?? 2 * 1024 * 1024;
    this.maxEntries = options.maxEntries ?? 8;
    this.maxEntryBytes = options.maxEntryBytes ?? 768 * 1024;
  }

  request<T>({
    factory,
    key,
    mode,
    timeoutMs,
  }: {
    factory: (signal: AbortSignal) => Promise<T>;
    key: string;
    mode: GetRequestMode;
    timeoutMs: number;
  }): Promise<T> {
    this.pruneExpired();

    const cached = this.cache.get(key);
    if (cached) {
      if (mode === 'foreground') this.deleteCacheEntry(key);
      return Promise.resolve(cached.value as T);
    }

    let shared = this.inFlight.get(key);
    if (shared) {
      if (mode === 'foreground') shared.foreground = true;
      shared.extendDeadline(timeoutMs);
      return withConsumerTimeout(shared.promise as Promise<T>, timeoutMs, key);
    }

    const abortController = new AbortController();
    const startedAt = Date.now();
    let hardTimeoutMs = 0;
    let hardTimeout: ReturnType<typeof globalThis.setTimeout> | undefined;
    const extendDeadline = (budget: number) => {
      const nextBudget = Number.isFinite(budget) && budget > 0
        ? Math.max(this.hardTimeoutMs, budget)
        : this.hardTimeoutMs;
      if (nextBudget <= hardTimeoutMs) return;
      hardTimeoutMs = nextBudget;
      globalThis.clearTimeout(hardTimeout);
      // Anchor extensions to the original request: repeated consumers cannot
      // keep a shared request alive indefinitely with the same budget.
      hardTimeout = globalThis.setTimeout(() => {
        abortController.abort(new RequestTimeoutError(
          `Shared GET request exceeded ${hardTimeoutMs}ms: ${key}`,
        ));
      }, Math.max(0, startedAt + hardTimeoutMs - Date.now()));
    };
    extendDeadline(timeoutMs);
    shared = {
      abortController,
      epoch: this.epoch,
      foreground: mode === 'foreground',
      promise: Promise.resolve(undefined),
      extendDeadline,
    };

    const entry = shared;
    entry.promise = Promise.resolve()
      .then(() => {
        if (abortController.signal.aborted) {
          throw abortController.signal.reason ?? new Error('GET request aborted');
        }
        return factory(abortController.signal);
      })
      .catch((error) => {
        if (abortController.signal.aborted && abortController.signal.reason instanceof Error) {
          throw abortController.signal.reason;
        }
        throw error;
      })
      .then((value) => {
        if (entry.epoch !== this.epoch) {
          if (entry.foreground) throw new RequestInvalidatedError(key);
          return value;
        }
        if (!entry.foreground) this.remember(key, value);
        return value;
      })
      .finally(() => {
        globalThis.clearTimeout(hardTimeout);
        if (this.inFlight.get(key) === entry) this.inFlight.delete(key);
      });

    this.inFlight.set(key, entry);
    return withConsumerTimeout(entry.promise as Promise<T>, timeoutMs, key);
  }

  invalidate(options: GetRequestInvalidationOptions = {}): void {
    this.epoch += 1;
    this.cache.clear();
    this.cacheBytes = 0;
    for (const entry of this.inFlight.values()) {
      if (entry.abortController.signal === options.preserveSignal) {
        entry.epoch = this.epoch;
        continue;
      }
      if (!entry.foreground) {
        entry.abortController.abort(new Error('Prefetch invalidated by a mutation or session change'));
      }
    }
  }

  snapshot(): GetRequestSnapshot {
    this.pruneExpired();
    let foregroundInFlight = 0;
    for (const entry of this.inFlight.values()) {
      if (entry.foreground) foregroundInFlight += 1;
    }
    return {
      cacheBytes: this.cacheBytes,
      cacheEntries: this.cache.size,
      epoch: this.epoch,
      foregroundInFlight,
      inFlight: this.inFlight.size,
      prefetchInFlight: this.inFlight.size - foregroundInFlight,
    };
  }

  private remember(key: string, value: unknown): void {
    const bytes = estimateBytes(value);
    if (!Number.isFinite(bytes) || bytes > this.maxEntryBytes || bytes > this.maxCacheBytes) return;

    this.deleteCacheEntry(key);
    while (
      this.cache.size >= this.maxEntries ||
      (this.cache.size > 0 && this.cacheBytes + bytes > this.maxCacheBytes)
    ) {
      const oldestKey = this.cache.keys().next().value as string | undefined;
      if (!oldestKey) break;
      this.deleteCacheEntry(oldestKey);
    }
    if (this.cacheBytes + bytes > this.maxCacheBytes) return;

    this.cache.set(key, {
      bytes,
      expiresAt: Date.now() + this.cacheTtlMs,
      value,
    });
    this.cacheBytes += bytes;
  }

  private pruneExpired(): void {
    const now = Date.now();
    for (const [key, entry] of this.cache) {
      if (entry.expiresAt <= now) this.deleteCacheEntry(key);
    }
  }

  private deleteCacheEntry(key: string): void {
    const entry = this.cache.get(key);
    if (!entry) return;
    this.cache.delete(key);
    this.cacheBytes = Math.max(0, this.cacheBytes - entry.bytes);
  }
}
