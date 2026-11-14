import {hostApiFetch as apiFetch} from './hostClient';
import {
  parseDefaultsActivationResponse,
  parseDefaultsSetupState,
  type DefaultsActivation,
  type DefaultsConfirmation,
  type DefaultsSetupState,
} from './defaultsSetupContract';

export * from './defaultsSetupContract';

export async function fetchDefaultsSetupState(
  options: {waitForRestart?: boolean; includeSourceAdditions?: boolean; timeoutMs?: number} = {},
): Promise<DefaultsSetupState> {
  // Restart may close the connection before a response arrives. Retry only
  // transport failures of this read; integrity/auth errors and POSTs are final.
  // A committed Profile can require a full cold Host capture on Windows.
  // Keep this read bounded without racing a still-running activation.
  const deadline = Date.now() + (options.timeoutMs ?? 300_000);
  const needsExtendedRead = options.waitForRestart || options.includeSourceAdditions;
  while (true) {
    try {
      return parseDefaultsSetupState(await apiFetch<unknown>(
        options.includeSourceAdditions
          ? '/api/setup/packs?include_source_additions=true' : '/api/setup/packs',
        {}, needsExtendedRead
          ? {timeoutMs: Math.max(1, deadline - Date.now())} : {},
      ));
    } catch (error) {
      const disconnected = error instanceof TypeError && [
        'Load failed', 'Failed to fetch', 'NetworkError when attempting to fetch resource.',
      ].includes(error.message);
      if (!options.waitForRestart || !disconnected || Date.now() + 500 >= deadline) throw error;
      await new Promise<void>((resolve) => setTimeout(resolve, 500));
    }
  }
}

export async function activateDefaultsProfile(
  confirmation: DefaultsConfirmation,
  options: {includeSourceAdditions?: boolean} = {},
): Promise<DefaultsActivation> {
  return parseDefaultsActivationResponse(
    await apiFetch<unknown>(options.includeSourceAdditions
      ? '/api/setup/packs/install?include_source_additions=true' : '/api/setup/packs/install', {
      method: 'POST',
      body: JSON.stringify({
        setup_api_version: 'io.tobkiri.setup-state.v4',
        operation_id: 'defaults.activate',
        confirmed: true,
        confirmation,
      }),
    }, {timeoutMs: 300_000}),
    confirmation,
  );
}
