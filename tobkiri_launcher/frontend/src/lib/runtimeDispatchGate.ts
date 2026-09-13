import type {RuntimeStatus} from './apiTypes';

export type RuntimeDispatchStatus = RuntimeStatus | 'unknown';

const PROFILE_RECONFIRMATION_STATUS = 'profile_reconfirmation_required';

let runtimeDispatchStatus: RuntimeDispatchStatus = 'unknown';

export class RuntimeDispatchBlockedError extends Error {
  readonly code: 'PROFILE_RECONFIRMATION_REQUIRED' | 'RUNTIME_DISPATCH_UNAVAILABLE';
  readonly method: string;
  readonly path: string;
  readonly status: RuntimeDispatchStatus;

  constructor(method: string, path: string, status: RuntimeDispatchStatus) {
    const reconfirmation = status === PROFILE_RECONFIRMATION_STATUS;
    super(reconfirmation
      ? `Profile reconfirmation is required before ${method} ${path} can run. `
        + 'Review and activate the exact Defaults v4 transaction in Setup first.'
      : `Runtime dispatch is unavailable before the Host publishes a verified v4 session and map. `
        + `Current status: ${status}.`);
    this.name = 'RuntimeDispatchBlockedError';
    this.code = reconfirmation
      ? 'PROFILE_RECONFIRMATION_REQUIRED'
      : 'RUNTIME_DISPATCH_UNAVAILABLE';
    this.method = method;
    this.path = path;
    this.status = status;
  }
}

export function setRuntimeDispatchStatus(status: RuntimeDispatchStatus): void {
  runtimeDispatchStatus = status;
}

export function getRuntimeDispatchStatus(): RuntimeDispatchStatus {
  return runtimeDispatchStatus;
}

/**
 * Keep all map-backed and PackVM requests fail-closed until the Host publishes
 * a verified dispatch session and contract map after Profile activation.
 */
export function assertRuntimeDispatchAllowed(method: string, path: string): void {
  if (runtimeDispatchStatus === 'runtime_ready') return;
  throw new RuntimeDispatchBlockedError(method, path, runtimeDispatchStatus);
}
