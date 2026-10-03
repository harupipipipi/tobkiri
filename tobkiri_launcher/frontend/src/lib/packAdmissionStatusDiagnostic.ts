export interface PackAdmissionStatusDiagnostic {
  code: string;
  summary: string;
}

// Native invoke errors may contain local paths, URLs or credentials. Never
// render them verbatim; only these fixed, non-sensitive messages are public.
const knownErrors = new Map<string, PackAdmissionStatusDiagnostic>([
  ['signed Pack trust status caller is unavailable', {
    code: 'PACK_STATUS_CALLER_UNAVAILABLE', summary: 'The native panel caller could not be identified.',
  }],
  ['signed Pack trust status requires the current Launcher panel', {
    code: 'PACK_STATUS_PANEL_MISMATCH', summary: 'The request did not come from the current Launcher panel.',
  }],
  ['Launcher Pack trust authentication is unavailable', {
    code: 'PACK_STATUS_AUTH_UNAVAILABLE', summary: 'Launcher authentication for the local Host is unavailable.',
  }],
  ['Launcher Pack trust client is unavailable', {
    code: 'PACK_STATUS_CLIENT_UNAVAILABLE', summary: 'The native Host client could not be initialized.',
  }],
  ['Local Host Pack trust status is unavailable', {
    code: 'PACK_STATUS_HOST_UNREACHABLE', summary: 'The native request could not reach the local Host.',
  }],
  ['Local Host Pack trust status returned an invalid response', {
    code: 'PACK_STATUS_INVALID_RESPONSE', summary: 'The local Host returned an unreadable status response.',
  }],
  ['Local Host Pack trust status was denied', {
    code: 'PACK_STATUS_HOST_DENIED', summary: 'The local Host rejected the status request.',
  }],
  ['Local Host Pack trust status returned no result', {
    code: 'PACK_STATUS_MISSING_RESULT', summary: 'The local Host did not return a status result.',
  }],
  ['Launcher Pack trust status task failed', {
    code: 'PACK_STATUS_TASK_FAILED', summary: 'The native status task could not complete.',
  }],
]);

export function packAdmissionStatusDiagnostic(error: unknown): PackAdmissionStatusDiagnostic {
  let message = '';
  try {
    if (typeof error === 'string') message = error;
    else if (error instanceof Error) {
      const value: unknown = Object.getOwnPropertyDescriptor(error, 'message')?.value;
      if (typeof value === 'string') message = value;
    }
  } catch {
    // Even malformed error objects must not interrupt the recovery surface.
  }
  const known = knownErrors.get(message);
  if (known) return { ...known };
  if (message.startsWith('signed_pack_admission_status not allowed.')) {
    return {
      code: 'PACK_STATUS_NATIVE_DENIED',
      summary: 'The native permission check rejected the status command.',
    };
  }
  return {
    code: 'PACK_STATUS_UNKNOWN',
    summary: 'The Pack status request failed. No raw diagnostic data is displayed.',
  };
}
