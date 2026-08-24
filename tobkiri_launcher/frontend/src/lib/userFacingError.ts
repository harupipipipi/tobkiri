import {recordClientDiagnostic} from './clientDiagnostics';

const TYPED_ERROR_CODES: Record<string, string> = {
  ApiContractError: 'API_CONTRACT_REJECTED',
  ApiRequestTimeoutError: 'REQUEST_TIMEOUT',
  ConfirmationPreDispatchError: 'CONFIRMATION_RETRYABLE',
  MutationBlockedError: 'MUTATION_BLOCKED',
  MutationResultUnknownError: 'MUTATION_UNKNOWN',
  PackVMLifecycleProtocolError: 'PACKVM_PROTOCOL_ERROR',
  RuntimeSurfaceError: 'RUNTIME_SURFACE_ERROR',
};

export function typedErrorCode(error: unknown): string {
  if (!(error instanceof Error)) return 'UNEXPECTED_ERROR';
  return TYPED_ERROR_CODES[error.name] ?? 'UNEXPECTED_ERROR';
}

export interface UserFacingError {
  message: string;
  code: string;
  diagnosticReference: string;
  technicalDetails: string;
}

/** Build a safe display error without retaining exception messages or payloads. */
export function createUserFacingError(
  error: unknown,
  fallback: string,
  operation: string,
): UserFacingError {
  const diagnostic = recordClientDiagnostic({
    code: 'ui.user_facing_error',
    operation,
    error,
  });
  const code = typedErrorCode(error);
  return {
    message: fallback,
    code,
    diagnosticReference: diagnostic.reference,
    technicalDetails: `${code}; diagnostic ${diagnostic.reference}`,
  };
}

/** Map an exception to a safe user message while retaining a typed diagnostic reference. */
export function formatUserFacingError(
  error: unknown,
  fallback: string,
  operation: string,
): string {
  const safeError = createUserFacingError(error, fallback, operation);
  return `${safeError.message} (${safeError.technicalDetails})`;
}
