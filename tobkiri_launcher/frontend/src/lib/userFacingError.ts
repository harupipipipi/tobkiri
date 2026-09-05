import {recordClientDiagnostic} from './clientDiagnostics';

const TYPED_ERROR_CODES: Record<string, string> = {
  ApiContractError: 'API_CONTRACT_REJECTED',
  ApiRequestTimeoutError: 'REQUEST_TIMEOUT',
  MutationBlockedError: 'MUTATION_BLOCKED',
  MutationResultUnknownError: 'MUTATION_UNKNOWN',
  PackVMLifecycleProtocolError: 'PACKVM_PROTOCOL_ERROR',
  RuntimeSurfaceError: 'RUNTIME_SURFACE_ERROR',
};

function typedErrorCode(error: unknown): string {
  if (!(error instanceof Error)) return 'UNEXPECTED_ERROR';
  return TYPED_ERROR_CODES[error.name] ?? 'UNEXPECTED_ERROR';
}

/** Map an exception to a safe user message while retaining a typed diagnostic reference. */
export function formatUserFacingError(
  error: unknown,
  fallback: string,
  operation: string,
): string {
  const diagnostic = recordClientDiagnostic({
    code: 'ui.user_facing_error',
    operation,
    error,
  });
  return `${fallback} (${typedErrorCode(error)}; diagnostic ${diagnostic.reference})`;
}
