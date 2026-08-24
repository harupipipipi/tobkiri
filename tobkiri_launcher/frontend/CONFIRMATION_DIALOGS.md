# Confirmation dialog contract

Tobkiri Launcher confirmation dialogs use `DialogConfig` and the finite
`DialogConfirmationState` model in `src/lib/dialogConfirmation.ts`.

Every dialog names the affected object and action. Confirmation progresses
through `idle`, `pending`, `success`, `recoverable_error`, `conflict`, or
`terminal_error`. A recoverable failure preserves the dialog context and offers
Retry and Cancel. A conflict can expose an authoritative status refresh instead
of repeating the mutation. Terminal and unknown-result failures never offer a
blind retry.

Only a `ConfirmationPreDispatchError` proves a request was never dispatched and
therefore permits Retry after a transport failure. Unclassified network and
timeout errors fail closed as unknown outcomes.

`onConflict` is reserved for an authoritative read-only status refresh. If
that lookup fails, **Retry status** repeats only the read and never calls the
original mutation again.

Unsafe requests must retain a stable request identity and reconcile ambiguous
transport outcomes through the Host operation-status projection before the
dialog receives a `MutationResultUnknownError`. Callers must not turn an unknown
outcome into an implicit second request or bypass PackVM, ProfileLock,
ResolvedPlan, or Authority Kernel checks.

Raw exceptions and response payloads are never rendered or copied. The dialog
records a bounded client diagnostic and exposes only its typed safe code and
reference under **Technical details**. Error feedback belongs to the dialog;
callers using `errorSurface: 'dialog'` must not also emit a failure toast.

Backdrop and Escape cancellation are blocked during a pending mutation unless
the caller supplies `pendingCancellation`. On failure, focus moves to the error
once. Closing restores focus to the element that opened the dialog.
