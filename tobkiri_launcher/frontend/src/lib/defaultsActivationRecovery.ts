import {ApiContractError} from './apiTransport';
import type {DefaultsSetupState} from './defaultsSetup';

const ACTIVATION_RESTART_DEADLINE_MS = 60_000;
const ACTIVATION_RESTART_RETRY_DELAY_MS = 500;

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

type DefaultsActivationRecoveryOptions = {
  /**
   * An activation confirmation may have crossed the durable boundary. This
   * is set for either a valid receipt or an indeterminate POST result, so a
   * later read failure cannot make the confirmation replayable.
   */
  readonly committedActivation?: boolean;
  /** Test-only time injection; production uses the bounded Host handoff. */
  readonly restartDeadlineMs?: number;
  readonly retryDelayMs?: number;
  readonly now?: () => number;
  readonly wait?: (milliseconds: number) => Promise<void>;
};

function restartVerificationPendingError(): Error {
  return new Error(
    'Activation is committed, but the Host restart has not published the active Profile yet.',
  );
}

function unexpectedRestartStateError(state: DefaultsSetupState): Error {
  return new Error(
    `Activation is committed, but restart verification returned ${state.state}.`,
  );
}

function waitForRestart(milliseconds: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

function isCommittedActivationHandoff(error: ApiContractError): boolean {
  if (!error.data || typeof error.data !== 'object' || Array.isArray(error.data)) {
    return false;
  }
  const data = error.data as Record<string, unknown>;
  return Object.keys(data).length === 2
    && data.state === 'activation_committed'
    && data.restart_required === true;
}

/**
 * Re-read Host authority after an activation attempt and reconcile an active
 * profile. This operation never submits an activation confirmation.
 */
export async function recoverDefaultsActivation(
  dependencies: DefaultsActivationRecoveryDependencies,
  options: DefaultsActivationRecoveryOptions = {},
): Promise<DefaultsActivationRecoveryResult> {
  const committedActivation = options.committedActivation ?? false;
  const now = options.now ?? Date.now;
  const wait = options.wait ?? waitForRestart;
  const retryDelayMs = options.retryDelayMs ?? ACTIVATION_RESTART_RETRY_DELAY_MS;
  const deadline = now() + (options.restartDeadlineMs ?? ACTIVATION_RESTART_DEADLINE_MS);

  while (true) {
    try {
      const state = await dependencies.fetchAuthoritativeSetup();
      if (state.state === 'active') {
        try {
          await dependencies.reconcileActiveRuntime();
          return {state, activationCommitted: true, error: null};
        } catch (error) {
          return {state, activationCommitted: true, error};
        }
      }
      if (!committedActivation) {
        return {state, activationCommitted: false, error: null};
      }
      if (state.state !== 'review_required') {
        return {state, activationCommitted: true, error: unexpectedRestartStateError(state)};
      }
      const remaining = deadline - now();
      if (remaining <= 0) {
        return {state, activationCommitted: true, error: restartVerificationPendingError()};
      }
      await wait(Math.min(retryDelayMs, remaining));
    } catch (error) {
      // A read-only recovery that has not followed an activation submission
      // must not manufacture a committed state from its own GET failure.
      // Conversely, a receipt or indeterminate POST stays locked so callers
      // can continue verification without replaying the confirmation.
      return {state: null, activationCommitted: committedActivation, error};
    }
  }
}

async function recoverExplicitActivationFailure(
  dependencies: DefaultsActivationRecoveryDependencies,
  submissionError: ApiContractError,
): Promise<DefaultsActivationRecoveryResult> {
  try {
    const state = await dependencies.fetchAuthoritativeSetup();
    // A rejected POST is never upgraded to a success by a concurrently
    // observed active state. A later, separate setup read owns that state.
    return {
      state: state.state === 'active' ? null : state,
      activationCommitted: false,
      error: submissionError,
    };
  } catch {
    return {state: null, activationCommitted: false, error: submissionError};
  }
}

/**
 * Submit exactly once, then resolve the outcome from authoritative Setup.
 * A lost response is therefore recovered without replaying the confirmation.
 */
export async function activateDefaultsWithRecovery(
  dependencies: DefaultsActivationRecoveryDependencies & {
    readonly submitActivation: () => Promise<unknown>;
    /** Surface the trusted receipt before cold-restart verification begins. */
    readonly onActivationCommitted?: () => void;
  },
): Promise<DefaultsActivationRecoveryResult> {
  try {
    await dependencies.submitActivation();
    dependencies.onActivationCommitted?.();
    return recoverDefaultsActivation(dependencies, {committedActivation: true});
  } catch (error) {
    if (error instanceof ApiContractError) {
      if (isCommittedActivationHandoff(error)) {
        dependencies.onActivationCommitted?.();
        return recoverDefaultsActivation(dependencies, {committedActivation: true});
      }
      return recoverExplicitActivationFailure(dependencies, error);
    }
    // Transport and response-integrity failures leave the durable outcome
    // unknown, so preserve the existing read-only recovery path.
    // An indeterminate POST result may have crossed the durable boundary.
    // Keep that confirmation locked even if the immediate authoritative GET
    // also fails; a later recovery performs reads only.
    const recovered = await recoverDefaultsActivation(dependencies, {
      committedActivation: true,
    });
    return {
      ...recovered,
      error: recovered.error
        ?? (recovered.state?.state === 'review_required' ? error : null),
    };
  }
}
