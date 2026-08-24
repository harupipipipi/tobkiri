import {ApiContractError} from './api';
import {MutationResultUnknownError} from './mutationJournal';
import {createUserFacingError} from './userFacingError';

export type ConfirmationFailureStatus =
  | 'recoverable_error'
  | 'conflict'
  | 'terminal_error';

export interface ConfirmationFailure {
  status: ConfirmationFailureStatus;
  message: string;
  guidance: string;
  technicalDetails: string;
  retryAllowed: boolean;
}

export type DialogConfirmationState =
  | {status: 'idle'}
  | {status: 'pending'; phase: 'confirm' | 'conflict_refresh'}
  | {status: 'success'}
  | ({status: ConfirmationFailureStatus} & {
    failure: ConfirmationFailure;
    source: 'confirm' | 'conflict_refresh';
  });

export interface DialogPendingCancellation {
  cancel: () => void | Promise<void>;
}

export interface DialogConfig {
  title: string;
  message: string;
  objectLabel: string;
  actionLabel: string;
  onConfirm: () => void | Promise<void>;
  onConflict?: () => void | Promise<void>;
  pendingCancellation?: DialogPendingCancellation;
  confirmText?: string;
  confirmPendingText?: string;
  cancelText?: string;
}

/** Mark a failure that is proven to have happened before mutation dispatch. */
export class ConfirmationPreDispatchError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'ConfirmationPreDispatchError';
  }
}

function apiErrorText(error: ApiContractError): string {
  const dataCode = (
    error.data
    && typeof error.data === 'object'
    && !Array.isArray(error.data)
    && typeof (error.data as Record<string, unknown>).code === 'string'
  ) ? (error.data as Record<string, string>).code : '';
  return `${error.status ?? ''} ${dataCode} ${error.message}`.toLowerCase();
}

/** Classify confirmation failures without exposing raw provider or Host details. */
export function classifyConfirmationFailure(
  error: unknown,
  options: {preDispatchRetrySafe?: boolean} = {},
): ConfirmationFailure {
  const safe = createUserFacingError(
    error,
    'The confirmation could not be completed.',
    'dialog.confirm',
  );

  if (error instanceof MutationResultUnknownError) {
    return {
      status: 'terminal_error',
      message: 'Tobkiri could not confirm whether the action completed.',
      guidance: 'Authoritative status was checked. Close this dialog and review the current state; the action will not be repeated automatically.',
      technicalDetails: safe.technicalDetails,
      retryAllowed: false,
    };
  }

  if (error instanceof ConfirmationPreDispatchError) {
    return {
      status: 'recoverable_error',
      message: safe.message,
      guidance: 'Your selection is preserved. Retry when ready, or cancel without applying the action.',
      technicalDetails: safe.technicalDetails,
      retryAllowed: true,
    };
  }

  if (error instanceof ApiContractError) {
    const text = apiErrorText(error);
    if (/\b409\b|conflict|already[_ -]?(?:settled|revoked|completed)/.test(text)) {
      return {
        status: 'conflict',
        message: 'The item changed before this action could be completed.',
        guidance: 'Refresh authoritative status before deciding what to do next.',
        technicalDetails: safe.technicalDetails,
        retryAllowed: false,
      };
    }
    if (/\b401\b|\b403\b|unauthori[sz]ed|forbidden|auth.*expired|session.*expired/.test(text)) {
      return {
        status: 'terminal_error',
        message: 'Your authorization is no longer valid for this action.',
        guidance: 'Close this dialog, restore access, then start the action again.',
        technicalDetails: safe.technicalDetails,
        retryAllowed: false,
      };
    }
    if (/\b400\b|\b404\b|\b422\b|invalid|validation|not[_ -]?found/.test(text)) {
      return {
        status: 'terminal_error',
        message: safe.message,
        guidance: 'Cancel and review the selected item or entered choices before starting again.',
        technicalDetails: safe.technicalDetails,
        retryAllowed: false,
      };
    }
  }

  if (options.preDispatchRetrySafe) {
    return {
      status: 'recoverable_error',
      message: safe.message,
      guidance: 'The status lookup can be retried safely, or you can close this dialog.',
      technicalDetails: safe.technicalDetails,
      retryAllowed: true,
    };
  }

  const errorText = error instanceof Error ? `${error.name} ${error.message}`.toLowerCase() : '';
  if (/abort|timed? ?out|timeout|failed to fetch|network|load failed/.test(errorText)) {
    return {
      status: 'terminal_error',
      message: 'Tobkiri could not confirm whether the action completed.',
      guidance: 'Close this dialog and review the current state; the action will not be repeated automatically.',
      technicalDetails: safe.technicalDetails,
      retryAllowed: false,
    };
  }

  return {
    status: 'terminal_error',
    message: safe.message,
    guidance: 'Close this dialog and review the selected item before starting the action again.',
    technicalDetails: safe.technicalDetails,
    retryAllowed: false,
  };
}
