export interface ClientDiagnostic {
  reference: string;
  code: string;
  operation: string;
  error_type: string;
  created_at: number;
}

export interface ClientDiagnosticInput {
  code: string;
  operation: string;
  error?: unknown;
}

const MAX_DIAGNOSTICS = 64;
const diagnostics: ClientDiagnostic[] = [];
let nextReference = 0;

function errorType(error: unknown): string {
  if (error instanceof Error) return error.name || 'Error';
  if (error === null) return 'null';
  return typeof error;
}

/** Record a bounded, non-sensitive diagnostic for a recoverable client failure. */
export function recordClientDiagnostic(input: ClientDiagnosticInput): ClientDiagnostic {
  nextReference += 1;
  const diagnostic: ClientDiagnostic = {
    reference: `diag-${nextReference.toString(36)}`,
    code: input.code,
    operation: input.operation,
    error_type: errorType(input.error),
    created_at: Date.now(),
  };
  diagnostics.unshift(diagnostic);
  if (diagnostics.length > MAX_DIAGNOSTICS) diagnostics.length = MAX_DIAGNOSTICS;
  return diagnostic;
}

/** Return recent diagnostics without exposing mutable internal state. */
export function listClientDiagnostics(): readonly ClientDiagnostic[] {
  return diagnostics.map((diagnostic) => ({...diagnostic}));
}

/** Clear diagnostics between isolated client sessions or tests. */
export function clearClientDiagnostics(): void {
  diagnostics.length = 0;
}
