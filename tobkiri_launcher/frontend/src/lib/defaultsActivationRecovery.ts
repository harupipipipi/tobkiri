import type {DefaultsSetupState} from './defaultsSetup';

export type DefaultsActivationRecoveryResult = {
  readonly state: DefaultsSetupState | null;
  /** True while activation is committed or its outcome is still unknown. */
  readonly activationCommitted: boolean;
  readonly error: unknown | null;
};

export type DefaultsActivationRecoveryDependencies = {
  readonly fetchAuthoritativeSetup: () => Promise<DefaultsSetupState>;
  readonly reconcileActiveRuntime: () => Promise<void>;
};

/**
 * Re-read Host authority after an activation attempt and reconcile an active
 * profile. This operation never submits an activation confirmation.
 */
export async function recoverDefaultsActivation(
  dependencies: DefaultsActivationRecoveryDependencies,
): Promise<DefaultsActivationRecoveryResult> {
  try {
    const state = await dependencies.fetchAuthoritativeSetup();
    if (state.state !== 'active') {
      return {state, activationCommitted: false, error: null};
    }
    try {
      await dependencies.reconcileActiveRuntime();
      return {state, activationCommitted: true, error: null};
    } catch (error) {
      return {state, activationCommitted: true, error};
    }
  } catch (error) {
    return {state: null, activationCommitted: true, error};
  }
}

/**
 * Submit exactly once, then resolve the outcome from authoritative Setup.
 * A lost response is therefore recovered without replaying the confirmation.
 */
export async function activateDefaultsWithRecovery(
  dependencies: DefaultsActivationRecoveryDependencies & {
    readonly submitActivation: () => Promise<unknown>;
  },
): Promise<DefaultsActivationRecoveryResult> {
  let submissionError: unknown | null = null;
  try {
    await dependencies.submitActivation();
  } catch (error) {
    submissionError = error;
  }

  const recovered = await recoverDefaultsActivation(dependencies);
  if (recovered.state?.state === 'review_required') {
    return {
      ...recovered,
      activationCommitted: false,
      error: submissionError ?? recovered.error,
    };
  }
  return {
    ...recovered,
    error: recovered.error ?? (recovered.state ? null : submissionError),
  };
}
