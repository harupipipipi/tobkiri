import {useEffect, useRef, useState, type ReactNode} from 'react';
import {
  cancelPackVM,
  cleanupPackVM,
  consentPackVM,
  fetchPackVMProgress,
  preparePackVM,
  provisionPackVM,
  stopPackVM,
} from '@/src/lib/api';
import type {
  ApiPackVMConsent,
  ApiPackVMDoctor,
  ApiPackVMOperation,
  ApiPackVMProvisioningPlan,
} from '@/src/lib/apiTypes';
import {
  cleanupConfirmationForInstance,
  clearPackVMOperationId,
  formatPackVMBytes,
  formatPackVMRecoveryError,
  isCanonicalPackVMOperationId,
  operationIsPolling,
  operationStatusLabel,
  readPackVMOperationId,
  stopConfirmationForInstance,
  userSafePackVMError,
  writePackVMOperationId,
} from '@/src/lib/packvmLifecycle';
import {useAppStore} from '@/src/store';
import {Badge} from '@/src/components/ui/Badge';
import {Button} from '@/src/components/ui/Button';
import {Card, CardContent, CardDescription, CardHeader, CardTitle} from '@/src/components/ui/Card';
import {Input} from '@/src/components/ui/Input';
import {PackDiagnostics} from './PackDiagnostics';

type PendingAction = 'prepare' | 'consent' | 'provision' | 'status' | 'cancel' | 'stop' | 'cleanup';

function statusVariant(doctor: ApiPackVMDoctor | null): 'success' | 'warning' | 'secondary' {
  if (doctor?.ready) return 'success';
  if (doctor) return 'warning';
  return 'secondary';
}

function safeUserError(error: unknown, fallback: string): string {
  const message = userSafePackVMError(error);
  return message || fallback;
}

function digestRow(label: string, value: string): ReactNode {
  return (
    <div>
      <dt className="font-medium text-text-main">{label}</dt>
      <dd className="mt-1 break-all font-mono">{value}</dd>
    </div>
  );
}

function failureDiagnostic(operation: ApiPackVMOperation): ReactNode {
  const diagnostic = operation.diagnostic;
  if (!operation.error_type && !diagnostic) return null;
  return (
    <dl
      className="mt-3 grid gap-2 rounded-lg border border-red-300/50 bg-red-500/5 p-3 text-xs text-text-muted sm:grid-cols-2"
      aria-label="Typed PackVM failure diagnostic"
    >
      {operation.error_type ? (
        <div>
          <dt className="font-medium text-text-main">Failure type</dt>
          <dd className="mt-1 break-all font-mono">{userSafePackVMError(operation.error_type)}</dd>
        </div>
      ) : null}
      {diagnostic ? (
        <>
          <div>
            <dt className="font-medium text-text-main">Diagnostic code</dt>
            <dd className="mt-1 break-all font-mono">{userSafePackVMError(diagnostic.code)}</dd>
          </div>
          <div>
            <dt className="font-medium text-text-main">Stage</dt>
            <dd className="mt-1 break-all font-mono">{userSafePackVMError(diagnostic.stage)}</dd>
          </div>
          <div>
            <dt className="font-medium text-text-main">Process result</dt>
            <dd className="mt-1 break-all font-mono">
              {userSafePackVMError(diagnostic.kind)}
              {diagnostic.exit_code === null ? '' : ` (${diagnostic.exit_code})`}
            </dd>
          </div>
          {diagnostic.stderr ? (
            <div className="sm:col-span-2">
              <dt className="font-medium text-text-main">Host diagnostic</dt>
              <dd className="mt-1 whitespace-pre-wrap break-words">{userSafePackVMError(diagnostic.stderr)}</dd>
            </div>
          ) : null}
        </>
      ) : null}
    </dl>
  );
}

export function PackVMLifecyclePanel() {
  const doctor = useAppStore((state) => state.packVmDoctor);
  const doctorLoading = useAppStore((state) => state.packVmDoctorLoading);
  const packVmError = useAppStore((state) => state.packVmError);
  const frontendCatalog = useAppStore((state) => state.frontendCatalog);
  const refreshPackVMDoctor = useAppStore((state) => state.refreshPackVMDoctor);
  const setPackVMDoctor = useAppStore((state) => state.setPackVMDoctor);
  const [plan, setPlan] = useState<ApiPackVMProvisioningPlan | null>(null);
  const [consent, setConsent] = useState<ApiPackVMConsent | null>(null);
  const [operation, setOperation] = useState<ApiPackVMOperation | null>(null);
  const [consentChecked, setConsentChecked] = useState(false);
  const [cleanupText, setCleanupText] = useState('');
  const [cleanupRequested, setCleanupRequested] = useState(false);
  const [restoring, setRestoring] = useState(false);
  const [pendingAction, setPendingAction] = useState<PendingAction | null>(null);
  const [lifecycleError, setLifecycleError] = useState<string | null>(null);
  const actionRef = useRef<PendingAction | null>(null);
  const restoreAttemptedRef = useRef(false);
  const settledOperationRef = useRef<string | null>(null);

  const beginAction = (action: PendingAction): boolean => {
    if (actionRef.current) return false;
    actionRef.current = action;
    setPendingAction(action);
    setLifecycleError(null);
    return true;
  };

  const finishAction = () => {
    actionRef.current = null;
    setPendingAction(null);
  };

  const verifyDoctorAfterSuccess = async (nextOperation: ApiPackVMOperation) => {
    if (settledOperationRef.current === nextOperation.operation_id) return;
    settledOperationRef.current = nextOperation.operation_id;
    const attested = await refreshPackVMDoctor();
    if (!attested?.ready) {
      setLifecycleError(
        'Provisioning reported success, but healthy PackVM attestation was not confirmed. Pack operations remain unavailable.',
      );
    }
  };

  const acceptOperation = async (
    nextOperation: ApiPackVMOperation,
    expectedOperationId: string,
  ) => {
    if (
      nextOperation.operation_id !== expectedOperationId
      || (
        nextOperation.operation_kind === 'provision'
        && plan
        && nextOperation.plan_digest !== plan.plan_digest
      )
    ) {
      clearPackVMOperationId();
      setPlan(null);
      setConsent(null);
      setConsentChecked(false);
      setOperation(null);
      throw new Error('PackVM returned a stale or tampered operation record.');
    }
    setOperation(nextOperation);
    writePackVMOperationId(nextOperation.operation_id);
    if (nextOperation.state === 'succeeded' && nextOperation.operation_kind === 'provision') {
      await verifyDoctorAfterSuccess(nextOperation);
    } else if (nextOperation.state === 'succeeded' && nextOperation.operation_kind === 'cleanup') {
      const result = nextOperation.result;
      const currentDoctor = doctor ?? await refreshPackVMDoctor();
      if (!result || !currentDoctor || result.instance !== currentDoctor.instance) {
        throw new Error('PackVM returned a stale or tampered cleanup result.');
      }
      setPackVMDoctor({
        ...currentDoctor,
        ready: false,
        reason: result.missing
          ? 'PackVM cleanup confirmed the managed instance was already absent.'
          : 'PackVM instance was cleaned up.',
        attestation_digest: null,
      });
      setPlan(null);
      setConsent(null);
      setCleanupRequested(false);
      setCleanupText('');
    }
  };

  const resumeOperation = async (operationId: string, action: PendingAction = 'status') => {
    if (!isCanonicalPackVMOperationId(operationId) || !beginAction(action)) return;
    try {
      const nextOperation = await fetchPackVMProgress(operationId);
      await acceptOperation(nextOperation, operationId);
    } catch (error) {
      const message = error instanceof Error ? error.message : '';
      if (/operation_id is unknown|unknown.*operation|operation.*unknown|operation.*not found|not found|stale or tampered|session.*(?:mismatch|invalid)|different session/i.test(message)) {
        clearPackVMOperationId();
        setPlan(null);
        setConsent(null);
        setConsentChecked(false);
        setOperation(null);
      }
      setLifecycleError(safeUserError(
        error,
        'The saved PackVM operation could not be resumed. Prepare a new plan before continuing.',
      ));
    } finally {
      finishAction();
    }
  };

  useEffect(() => {
    void refreshPackVMDoctor();
  }, [refreshPackVMDoctor]);

  useEffect(() => {
    if (restoreAttemptedRef.current) return;
    restoreAttemptedRef.current = true;
    const operationId = readPackVMOperationId();
    if (!operationId) return;
    setRestoring(true);
    void resumeOperation(operationId).finally(() => setRestoring(false));
  }, []);

  useEffect(() => {
    if (!operation || pendingAction || !operationIsPolling(operation.state)) return;
    const timer = window.setTimeout(() => {
      void resumeOperation(operation.operation_id);
    }, 750);
    return () => window.clearTimeout(timer);
  }, [operation?.operation_id, operation?.state, pendingAction]);

  const handlePrepare = async () => {
    if (!beginAction('prepare')) return;
    try {
      const nextPlan = await preparePackVM();
      setPlan(nextPlan);
      setConsent(null);
      setConsentChecked(false);
      setOperation(null);
      clearPackVMOperationId();
      settledOperationRef.current = null;
    } catch (error) {
      setLifecycleError(safeUserError(error, 'PackVM provisioning plan could not be prepared.'));
    } finally {
      finishAction();
    }
  };

  const handleConsent = async () => {
    if (!plan || !consentChecked || !beginAction('consent')) return;
    try {
      const nextConsent = await consentPackVM({
        plan_digest: plan.plan_digest,
        ceremony_nonce: plan.ceremony_nonce,
        confirmation: plan.confirmation,
        approve_image_download: consentChecked,
      });
      if (
        nextConsent.plan_digest !== plan.plan_digest
        || nextConsent.image_digest !== plan.image_digest
        || nextConsent.image_size_bytes !== plan.image_size_bytes
        || nextConsent.image_download_approved !== consentChecked
      ) {
        throw new Error('PackVM returned consent for a different pinned plan.');
      }
      setConsent(nextConsent);
    } catch (error) {
      setLifecycleError(safeUserError(error, 'PackVM consent was denied.'));
    } finally {
      finishAction();
    }
  };

  const handleProvision = async () => {
    if (!planIsAvailable || !plan || !consent || !beginAction('provision')) return;
    try {
      const operationId = globalThis.crypto?.randomUUID?.();
      if (!operationId || !isCanonicalPackVMOperationId(operationId)) {
        throw new Error('Tobkiri could not create a canonical PackVM operation identity.');
      }
      const nextOperation = await provisionPackVM({
        consent_id: consent.consent_id,
        operation_id: operationId,
      });
      await acceptOperation(nextOperation, operationId);
    } catch (error) {
      setLifecycleError(safeUserError(error, 'PackVM provisioning could not start.'));
    } finally {
      finishAction();
    }
  };

  const handleCancel = async () => {
    if (!operation || operation.state !== 'queued' || !beginAction('cancel')) return;
    try {
      const nextOperation = await cancelPackVM(operation.operation_id);
      if (nextOperation.state !== 'cancelled') {
        throw new Error('PackVM did not confirm cancellation.');
      }
      await acceptOperation(nextOperation, operation.operation_id);
    } catch (error) {
      setLifecycleError(safeUserError(error, 'PackVM cancellation was denied.'));
    } finally {
      finishAction();
    }
  };

  const handleStop = async () => {
    if (!doctor || !beginAction('stop')) return;
    try {
      const nextDoctor = await stopPackVM(stopConfirmationForInstance(doctor.instance));
      setPackVMDoctor(nextDoctor);
      setCleanupRequested(false);
      setCleanupText('');
    } catch (error) {
      setLifecycleError(safeUserError(error, 'PackVM stop was denied.'));
    } finally {
      finishAction();
    }
  };

  const handleCleanup = async () => {
    if (!doctor || cleanupText !== cleanupConfirmationForInstance(doctor.instance)) return;
    if (!beginAction('cleanup')) return;
    try {
      const cleanupOperationId = globalThis.crypto?.randomUUID?.();
      if (!cleanupOperationId || !isCanonicalPackVMOperationId(cleanupOperationId)) {
        throw new Error('Tobkiri could not create a canonical cleanup operation identity.');
      }
      const sourceOperationId = operation?.operation_kind === 'provision'
        && (operation.state === 'failed' || operation.state === 'interrupted')
        ? operation.operation_id
        : null;
      const nextOperation = await cleanupPackVM(
        cleanupText,
        cleanupOperationId,
        sourceOperationId,
      );
      await acceptOperation(nextOperation, cleanupOperationId);
    } catch (error) {
      setLifecycleError(safeUserError(error, 'PackVM cleanup was denied.'));
    } finally {
      finishAction();
    }
  };

  const operationStatus = operation
    ? `${operation.operation_kind === 'cleanup' ? 'Cleanup' : 'Provisioning'}: ${operationStatusLabel(operation.state)}`
    : null;
  const cleanupConfirmation = doctor ? cleanupConfirmationForInstance(doctor.instance) : '';
  const hasActiveOperation = Boolean(operation && operationIsPolling(operation.state));
  const canPrepareNewPlan = Boolean(
    !doctor?.ready
    && !hasActiveOperation
    && (!operation || operation.state === 'failed' || operation.state === 'cancelled'),
  );
  const canPrepare = !doctor?.ready && !hasActiveOperation && !pendingAction;
  const planIsAvailable = plan?.runtime_path_status === 'ready'
    && plan.launcher_reason === null
    && plan.image_source !== 'unavailable';
  const canConsent = Boolean(
    planIsAvailable && consentChecked && !consent && !pendingAction,
  );
  const canProvision = Boolean(
    planIsAvailable && consent && !operation && !pendingAction,
  );

  return (
    <>
      <Card aria-labelledby="packvm-lifecycle-title">
        <CardHeader>
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <CardTitle id="packvm-lifecycle-title">PackVM lifecycle</CardTitle>
              <CardDescription>
                Provisioning is a Host-owned ceremony. Pack operations stay hidden until a fresh doctor check returns a healthy attestation.
              </CardDescription>
            </div>
            <Badge variant={statusVariant(doctor)} aria-live="polite">
              {doctor?.ready ? 'Healthy and attested' : doctor ? 'Not ready' : 'Not verified'}
            </Badge>
          </div>
        </CardHeader>
        <CardContent className="space-y-5">
          {doctorLoading ? (
            <p className="text-sm text-text-muted" role="status" aria-busy="true">
              Checking PackVM health…
            </p>
          ) : null}
          {packVmError ? (
            <p className="rounded-lg border border-amber-300 bg-amber-50 p-3 text-sm text-amber-800 dark:border-amber-900/40 dark:bg-amber-950/20 dark:text-amber-200" role="alert">
              {formatPackVMRecoveryError(
                packVmError,
                safeUserError(packVmError, 'PackVM readiness could not be verified.'),
              )}
            </p>
          ) : null}
          {lifecycleError ? (
            <p className="rounded-lg border border-red-300 bg-red-50 p-3 text-sm text-red-800 dark:border-red-900/40 dark:bg-red-950/20 dark:text-red-200" role="alert">
              {lifecycleError}
            </p>
          ) : null}

          {doctor ? (
            <div className="rounded-lg border border-border bg-bg-main p-4">
              <div className="flex flex-wrap items-center justify-between gap-3">
                <div>
                  <p className="text-sm font-medium text-text-main">Doctor readiness</p>
                  <p className="mt-1 text-xs text-text-muted">
                    {doctor.ready
                      ? 'The authenticated PackVM supervisor answered the readiness check.'
                      : doctor.reason
                        ? formatPackVMRecoveryError(
                          doctor.reason,
                          userSafePackVMError(doctor.reason),
                        )
                        : 'The authenticated PackVM supervisor is not ready.'}
                  </p>
                </div>
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => {
                    if (!beginAction('status')) return;
                    void refreshPackVMDoctor().finally(finishAction);
                  }}
                  disabled={Boolean(pendingAction)}
                  loading={pendingAction === 'status'}
                  aria-label="Run PackVM doctor again"
                >
                  Run doctor again
                </Button>
              </div>
              <dl className="mt-4 grid gap-3 text-xs text-text-muted sm:grid-cols-2">
                {digestRow('Backend', doctor.backend_id)}
                {digestRow('Instance', doctor.instance)}
                {digestRow('Platform', doctor.platform)}
                {doctor.attestation_digest
                  ? digestRow('Attestation digest', doctor.attestation_digest)
                  : null}
              </dl>
            </div>
          ) : null}

          {canPrepareNewPlan ? (
            <div className="rounded-lg border border-border p-4">
              <p className="text-sm font-medium text-text-main">Prepare a pinned provisioning plan</p>
              <p className="mt-1 text-sm leading-relaxed text-text-muted">
                Tobkiri will show the exact image source, size, digests, configuration, runner, and required disk space before asking for consent.
              </p>
              <Button
                className="mt-4"
                onClick={() => void handlePrepare()}
                disabled={!canPrepare}
                loading={pendingAction === 'prepare'}
              >
                {operation ? 'Prepare a new plan' : 'Prepare plan'}
              </Button>
            </div>
          ) : null}

          {plan ? (
            <div className="rounded-lg border border-border p-4">
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div>
                  <p className="text-sm font-medium text-text-main">Pinned plan</p>
                  <p className="mt-1 text-xs text-text-muted">
                    Review these Host-provided facts before consenting. The Launcher never displays the Host executable path.
                  </p>
                </div>
                <Badge variant="outline">{plan.architecture}</Badge>
              </div>
              <dl className="mt-4 grid gap-3 text-xs text-text-muted sm:grid-cols-2">
                {digestRow('Image source', plan.image_source)}
                {digestRow('Image size', formatPackVMBytes(plan.image_size_bytes))}
                {digestRow('Image digest', plan.image_digest)}
                {digestRow('Configuration digest', plan.config_digest)}
                {digestRow('Guest runner digest', plan.guest_runner_digest)}
                {digestRow('Host build digest', plan.host_build_digest)}
                {digestRow('Plan digest', plan.plan_digest)}
                {digestRow('Required disk space', plan.image_download_required
                  ? `${formatPackVMBytes(plan.image_size_bytes)} for the pinned image download`
                  : 'No image download required')}
              </dl>
              {plan.launcher_reason ? (
                <p className="mt-4 rounded-lg border border-amber-300 bg-amber-50 p-3 text-xs text-amber-800 dark:border-amber-900/40 dark:bg-amber-950/20 dark:text-amber-200" role="status">
                  {userSafePackVMError(plan.launcher_reason)}
                </p>
              ) : null}
              {planIsAvailable ? (
                <div className="mt-4 rounded-lg border border-border bg-bg-main p-3">
                  <p className="text-xs font-medium text-text-main">Exact confirmation phrase</p>
                  <p className="mt-1 break-all font-mono text-xs text-text-muted">
                    {userSafePackVMError(plan.confirmation)}
                  </p>
                </div>
              ) : null}
              {!consent && planIsAvailable ? (
                <div className="mt-4 space-y-3">
                  <label className="flex cursor-pointer items-start gap-3 text-sm text-text-main">
                    <input
                      type="checkbox"
                      className="mt-0.5 h-4 w-4 rounded border-border accent-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--ring-color)]"
                      checked={consentChecked}
                      onChange={(event) => setConsentChecked(event.target.checked)}
                      disabled={Boolean(pendingAction)}
                    />
                    <span>
                      I reviewed this exact plan and authorize the pinned image action shown above.
                    </span>
                  </label>
                  <Button
                    onClick={() => void handleConsent()}
                    disabled={!canConsent}
                    loading={pendingAction === 'consent'}
                  >
                    Record explicit consent
                  </Button>
                </div>
              ) : null}
            </div>
          ) : null}

          {consent ? (
            <div className="rounded-lg border border-emerald-300 bg-emerald-50 p-4 dark:border-emerald-900/40 dark:bg-emerald-950/20">
              <p className="text-sm font-medium text-emerald-800 dark:text-emerald-200">Plan consent recorded by Tobkiri</p>
              <p className="mt-1 text-xs text-emerald-700 dark:text-emerald-300">
                The consent is one-shot and is bound to the displayed plan digest. Provisioning can now be started once.
              </p>
              <Button
                className="mt-4"
                onClick={() => void handleProvision()}
                disabled={!canProvision}
                loading={pendingAction === 'provision'}
              >
                Provision PackVM
              </Button>
            </div>
          ) : null}

          {restoring ? (
            <p className="text-sm text-text-muted" role="status" aria-busy="true">
              Resuming the saved PackVM operation status…
            </p>
          ) : null}

          {operation ? (
            <div className="rounded-lg border border-border p-4" aria-busy={pendingAction === 'status'}>
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div>
                  <p className="text-sm font-medium text-text-main">
                    {operation.operation_kind === 'cleanup' ? 'Cleanup status' : 'Provisioning status'}
                  </p>
                  <p className="mt-1 break-all font-mono text-xs text-text-muted">
                    Operation {operation.operation_id}
                  </p>
                </div>
                <Badge variant={operation.state === 'succeeded' ? 'success' : operation.state === 'failed' ? 'destructive' : operation.state === 'interrupted' ? 'warning' : 'secondary'}>
                  {operationStatus}
                </Badge>
              </div>
              <ol
                className="mt-4 grid gap-2 text-xs text-text-muted sm:grid-cols-3"
                aria-label={operation.operation_kind === 'cleanup' ? 'Cleanup progress' : 'Provisioning progress'}
              >
                {(['queued', 'running', 'succeeded'] as const).map((state) => (
                  <li
                    key={state}
                    className={operation.state === state
                      ? 'rounded-md border border-accent bg-accent/10 p-2 font-medium text-text-main'
                      : operation.state === 'succeeded' && state !== 'succeeded'
                        ? 'rounded-md border border-emerald-300 bg-emerald-50 p-2 text-emerald-700 dark:border-emerald-900/40 dark:bg-emerald-950/20 dark:text-emerald-300'
                        : 'rounded-md border border-border p-2'}
                  >
                    {operationStatusLabel(state)}
                  </li>
                ))}
              </ol>
              {operation.error ? (
                <p className="mt-3 text-sm text-destructive" role="alert">
                  {userSafePackVMError(operation.error)}
                </p>
              ) : null}
              {operation.state === 'failed' ? failureDiagnostic(operation) : null}
              {operation.state === 'queued' ? (
                <Button
                  className="mt-4"
                  variant="outline"
                  onClick={() => void handleCancel()}
                  disabled={Boolean(pendingAction)}
                  loading={pendingAction === 'cancel'}
                >
                  Cancel queued provisioning
                </Button>
              ) : null}
              {operation.state === 'interrupted' ? (
                <div className="mt-4 rounded-lg border border-amber-300 bg-amber-50 p-3 dark:border-amber-900/40 dark:bg-amber-950/20">
                  <p className="text-sm text-amber-800 dark:text-amber-200">
                    The Launcher restarted while this operation was in flight. Resume status to reattach to the persisted operation; no new approval or fallback is created.
                  </p>
                  <Button
                    className="mt-3"
                    variant="outline"
                    onClick={() => void resumeOperation(operation.operation_id)}
                    disabled={Boolean(pendingAction)}
                    loading={pendingAction === 'status'}
                  >
                    Resume status
                  </Button>
                </div>
              ) : null}
            </div>
          ) : null}

          {doctor ? (
            <div className="flex flex-wrap items-center gap-3 border-t border-border pt-4">
              <Button
                variant="outline"
                onClick={() => void handleStop()}
                disabled={Boolean(pendingAction) || !doctor.instance}
                loading={pendingAction === 'stop'}
              >
                Stop PackVM
              </Button>
              {!cleanupRequested ? (
                <Button
                  variant="destructive"
                  onClick={() => setCleanupRequested(true)}
                  disabled={Boolean(pendingAction) || !doctor.instance}
                >
                  Clean up PackVM
                </Button>
              ) : null}
            </div>
          ) : null}

          {cleanupRequested && doctor ? (
            <div className="rounded-lg border border-red-300 bg-red-50 p-4 dark:border-red-900/40 dark:bg-red-950/20" role="group" aria-labelledby="packvm-cleanup-title">
              <p id="packvm-cleanup-title" className="text-sm font-medium text-red-800 dark:text-red-200">
                Confirm PackVM cleanup
              </p>
              <p className="mt-1 text-xs text-red-700 dark:text-red-300">
                This deletes only the authenticated PackVM instance. Type the exact phrase to continue.
              </p>
              <Input
                className="mt-3 font-mono"
                label="Cleanup confirmation"
                value={cleanupText}
                onChange={(event) => setCleanupText(event.target.value)}
                onInput={(event) => setCleanupText(event.currentTarget.value)}
                placeholder={cleanupConfirmation}
                autoComplete="off"
                spellCheck={false}
                disabled={Boolean(pendingAction)}
              />
              <div className="mt-3 flex flex-wrap gap-3">
                <Button
                  variant="destructive"
                  onClick={() => void handleCleanup()}
                  disabled={cleanupText !== cleanupConfirmation || Boolean(pendingAction)}
                  loading={pendingAction === 'cleanup'}
                >
                  Delete authenticated PackVM
                </Button>
                <Button
                  variant="outline"
                  onClick={() => {
                    setCleanupRequested(false);
                    setCleanupText('');
                  }}
                  disabled={Boolean(pendingAction)}
                >
                  Keep PackVM
                </Button>
              </div>
            </div>
          ) : null}

          {!doctor?.ready ? (
            <p className="text-xs text-text-muted" role="status">
              No Pack operation is invokable until the authenticated doctor reports ready with an attestation digest.
            </p>
          ) : null}
        </CardContent>
      </Card>
      <PackDiagnostics diagnostics={frontendCatalog?.diagnostics ?? []} title="PackVM catalog diagnostics" />
    </>
  );
}
