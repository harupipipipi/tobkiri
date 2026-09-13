import type {RuntimeStatus} from './apiTypes';

export type SetupVerificationState =
  | 'checking'
  | 'verified'
  | 'needs_setup'
  | 'needs_reconfirm'
  | 'denied';

export interface SetupVerificationInput {
  isSetupDone: boolean;
  runtimeReady: boolean;
  runtimeStatus: RuntimeStatus;
  runtimeDisconnected: boolean;
  defaultsBootstrapRequired: boolean;
  hostCatalogVerified?: boolean;
  profileCeremonyAvailable?: boolean;
}

/**
 * Resolve the only states that the panel may use to expose runtime routes.
 *
 * A ready flag is not sufficient by itself: it must agree with the typed
 * health status and a disconnected runtime must fail closed even when a
 * previous healthy value is still in memory.
 */
export function resolveSetupVerificationState(
  input: SetupVerificationInput,
): SetupVerificationState {
  if (input.runtimeDisconnected) return 'denied';
  if (input.defaultsBootstrapRequired) return 'needs_setup';
  if (input.hostCatalogVerified && input.profileCeremonyAvailable) {
    return 'verified';
  }
  if (!input.isSetupDone) return 'needs_setup';
  if (input.runtimeStatus === 'profile_reconfirmation_required') return 'needs_reconfirm';
  if (input.runtimeStatus === 'error') return 'denied';
  if (input.runtimeReady && input.runtimeStatus === 'runtime_ready') return 'verified';
  return 'checking';
}
