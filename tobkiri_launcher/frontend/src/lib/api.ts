/** Compatibility exports; production callers import their Host or Application client. */
export * from './apiTransport';
export * from './hostClient';
export * from './desktopHost';
export * from './defaultspackClient';
export {getRuntimeDispatchStatus, setRuntimeDispatchStatus} from './runtimeDispatchGate';

import {hostApiFetch} from './hostClient';
import {defaultspackApiFetch} from './defaultspackClient';
import type {ApiRequestPolicy} from './apiTransport';

/** Retain the old finite allowlist for callers migrating to an explicit client. */
export function apiFetch<T>(
  path: string,
  options: RequestInit = {},
  requestPolicy: ApiRequestPolicy = {},
): Promise<T> {
  const client = path.startsWith('/api/contracts/defaultspack/')
    ? defaultspackApiFetch : hostApiFetch;
  return client<T>(path, options, requestPolicy);
}

export function prefetchApiGet<T>(
  path: string,
  options: {timeoutMs?: number} = {},
): Promise<T> {
  return apiFetch<T>(path, {}, {
    mode: 'prefetch',
    timeoutMs: options.timeoutMs ?? 2_500,
  });
}
