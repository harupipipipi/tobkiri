import {useEffect, useMemo, useRef, useState} from 'react';
import {AlertTriangle, ArrowRight, CheckCircle2, LockKeyhole, PackagePlus, ShieldCheck, XCircle} from 'lucide-react';

import {Badge} from '@/src/components/ui/Badge';
import {Button} from '@/src/components/ui/Button';
import {CopyErrorButton} from '@/src/components/ui/CopyErrorButton';
import {Card, CardContent, CardDescription, CardHeader, CardTitle} from '@/src/components/ui/Card';
import {broadcastRuntimeSurfaceRefresh, type RuntimeSurfaceState} from '@/src/hooks/useRuntimeSurface';
import {
  classifyRuntimeSurfaceError,
  extractExactProfileCatalogSelectablePackIds,
  runtimeSurfaceErrorMessage,
  RuntimeSurfaceError,
  type RuntimeProfileCatalogEntry,
  type RuntimeProfileCatalogProjection,
  type RuntimeSurfaceErrorCode,
} from '@/src/lib/runtimeSurface';
import {
  assertProfileCandidateMatches,
  defaultProfileCeremonyClient,
  validateProfileActivateResult,
  validateProfileApproveResult,
  validateProfileResolveResult,
  validateProfileReviewResult,
  snapshotForProfileCeremony,
  type ProfileActivateResult,
  type ProfileApproveResult,
  type ProfileCeremonyClient,
  type ProfileResolveResult,
  type ProfileReviewResult,
} from '@/src/lib/profileCeremony';
import {refreshMountedRuntimeSurfaces} from '@/src/lib/runtimeSurfaceRefresh';
import {PINNED_FRONTEND_CONTRACT_MAP_ARTIFACT_DIGEST} from '@/src/lib/generatedFrontendContractMap';
import {
  beginMutation,
  completeMutation,
  isMutationResultUnknown,
  markMutationUnknown,
  listMutationJournal,
  MUTATION_UNKNOWN_MESSAGE,
  MutationBlockedError,
  type MutationJournalRecord,
} from '@/src/lib/mutationJournal';
import type {Pack} from '@/src/store';
import {reconcileMutationStatus} from '@/src/lib/operationStatus';
import {recordClientDiagnostic} from '@/src/lib/clientDiagnostics';

type CeremonyState = 'idle' | 'resolving' | 'resolved' | 'reviewing' | 'reviewed' | 'approving' | 'approved' | 'activating' | 'active' | 'result_unknown' | 'error';

function recordDigest(record: unknown, keys: string[]): string {
  if (!record || typeof record !== 'object' || Array.isArray(record)) return 'not published';
  const value = record as Record<string, unknown>;
  const key = keys.find((candidate) => typeof value[candidate] === 'string');
  return key ? String(value[key]) : 'not published';
}

function snapshotKey(snapshot: {profile_id: string; profile_revision: string; plan_digest: string}): string {
  return `${snapshot.profile_id}:${snapshot.profile_revision}:${snapshot.plan_digest}`;
}

const PROFILE_CONTROL_CONTRACT = 'tobkiri.host.control-presentation.v4';
const NO_ACTIVE_PROFILE_REVISION = 'sha256:edce803cae9e07be4b409a12b7c775320d8626a86e0ee7dd540738bf39b4aad5';
const NO_ACTIVE_PLAN_DIGEST = 'sha256:0ef670f236250f9e03a6f9a8c462de318bda850ed28a5b6cff5f591a170a264c';

function profileOperationId(step: unknown): string {
  if (step === 'resolving') return 'profile.change.resolve';
  if (step === 'reviewing') return 'profile.change.review';
  if (step === 'approving') return 'profile.change.approve';
  if (step === 'activating') return 'profile.change.activate';
  throw new Error('Profile ceremony status step is invalid.');
}

export function ProfileCeremonyPanel({
  surface,
  packs,
  loadPacks,
  client = defaultProfileCeremonyClient,
  onActivated,
  authoritativeSelection,
  catalogSurface,
  onBusyChange,
}: {
  surface: RuntimeSurfaceState<unknown>;
  packs: Pack[];
  loadPacks: () => Promise<void>;
  client?: ProfileCeremonyClient;
  onActivated?: (result: ProfileActivateResult) => Promise<void>;
  authoritativeSelection: {
    entry: RuntimeProfileCatalogEntry;
    catalogDigest: string;
    bundleLockDigest: string;
  };
  catalogSurface: RuntimeSurfaceState<RuntimeProfileCatalogProjection>;
  onBusyChange?: (busy: boolean) => void;
}) {
  const [selectedPackIds, setSelectedPackIds] = useState<string[]>([]);
  const [ceremonyState, setCeremonyState] = useState<CeremonyState>('idle');
  const [candidate, setCandidate] = useState<ProfileResolveResult | null>(null);
  const [reviewed, setReviewed] = useState<ProfileReviewResult | null>(null);
  const [approval, setApproval] = useState<ProfileApproveResult | null>(null);
  const [ceremonySnapshot, setCeremonySnapshot] = useState<string | null>(null);
  const [failure, setFailure] = useState<{code: RuntimeSurfaceErrorCode; message: string} | null>(null);
  const [unknownMutation, setUnknownMutation] = useState<MutationJournalRecord | null>(null);
  const requestVersion = useRef(0);
  const busyRef = useRef(false);

  const authoritativePackIds = useMemo(
    () => extractExactProfileCatalogSelectablePackIds(authoritativeSelection.entry),
    [authoritativeSelection.entry],
  );

  const catalogProjection = catalogSurface.data?.data ?? null;
  const catalogEntry = authoritativeSelection.entry;
  const catalogBindingStable = Boolean(
    catalogSurface.status === 'ready'
    && !catalogSurface.stale
    && catalogSurface.data
    && catalogProjection
    && catalogProjection.catalog_digest === authoritativeSelection.catalogDigest
    && catalogProjection.bundle_lock_digest === authoritativeSelection.bundleLockDigest
    && catalogProjection.profiles.some((entry) => (
      entry.profile_id === catalogEntry.profile_id
      && entry.definition.digest === catalogEntry.definition.digest
    )),
  );
  const selectedPackKey = selectedPackIds.slice().sort().join(',');
  const catalogPackRows = selectedPackIds.map((id) => packs.find((pack) => pack.id === id) ?? null);
  const catalogMissingPackIds = catalogPackRows.flatMap((pack, index) => pack ? [] : [selectedPackIds[index] ?? '']);
  const catalogIncompatiblePackIds = catalogPackRows.flatMap((pack, index) => {
    if (!pack) return [];
    const closureEntry = catalogEntry?.pack_closure.find((item) => item.pack_id === pack.id);
    const expectedArtifactDigest = closureEntry?.artifact_digest ?? pack.artifactDigest;
    if (
      pack.artifactDigest !== expectedArtifactDigest
      || !pack.installed
      || !pack.approved
      || !pack.enabled
    ) return [selectedPackIds[index] ?? pack.id];
    return [];
  });

  useEffect(() => {
    requestVersion.current += 1;
    busyRef.current = false;
    setCeremonyState('idle');
    setCandidate(null);
    setReviewed(null);
    setApproval(null);
    setCeremonySnapshot(null);
    setFailure(null);
    setUnknownMutation(null);
  }, [catalogEntry.profile_id, catalogEntry.definition.digest, authoritativeSelection.catalogDigest, authoritativeSelection.bundleLockDigest]);

  useEffect(() => {
    setSelectedPackIds(authoritativePackIds ?? []);
  }, [catalogEntry.profile_id, catalogEntry.definition.digest, authoritativePackIds]);

  useEffect(() => {
    const busy = ['resolving', 'reviewing', 'approving', 'activating'].includes(ceremonyState);
    onBusyChange?.(busy);
    return () => {
      onBusyChange?.(false);
    };
  }, [ceremonyState, onBusyChange]);

  const currentSnapshot = snapshotForProfileCeremony(surface.data);
  const predecessorSnapshot = currentSnapshot ?? (
    catalogProjection?.active_profile_id === null
      ? {
        profile_id: '',
        profile_revision: NO_ACTIVE_PROFILE_REVISION,
        plan_digest: NO_ACTIVE_PLAN_DIGEST,
      }
      : null
  );
  const ceremonyIsBusy = ['resolving', 'reviewing', 'approving', 'activating'].includes(ceremonyState);
  const desiredPackIds = selectedPackIds;
  const currentBindingKey = [
    predecessorSnapshot ? snapshotKey(predecessorSnapshot) : 'no-runtime-snapshot',
    catalogEntry.profile_id,
    catalogEntry.definition.digest,
    authoritativeSelection.catalogDigest,
    authoritativeSelection.bundleLockDigest,
    selectedPackKey,
  ].join(':');
  const currentBindingRef = useRef(currentBindingKey);
  currentBindingRef.current = currentBindingKey;
  const previousBindingKey = useRef(currentBindingKey);

  useEffect(() => {
    if (unknownMutation) return;
    const hydrated = listMutationJournal().find((record) => (
      record.state === 'unknown'
      && record.metadata.kind === 'profile.ceremony'
      && record.metadata.binding_key === currentBindingKey
    ));
    if (!hydrated) return;
    setUnknownMutation(hydrated);
    setFailure({code: 'TIMEOUT', message: MUTATION_UNKNOWN_MESSAGE});
    setCeremonyState('result_unknown');
  }, [currentBindingKey, unknownMutation]);

  useEffect(() => {
    if (previousBindingKey.current === currentBindingKey) return;
    previousBindingKey.current = currentBindingKey;
    requestVersion.current += 1;
    busyRef.current = false;
    setCeremonyState('idle');
    setCandidate(null);
    setReviewed(null);
    setApproval(null);
    setCeremonySnapshot(null);
    setFailure(null);
    setUnknownMutation(null);
  }, [currentBindingKey]);

  const catalogSelectionAvailable = Boolean(
    catalogEntry.available
    && selectedPackIds.length > 0
    && catalogBindingStable
    && catalogMissingPackIds.length === 0
    && catalogIncompatiblePackIds.length === 0,
  );
  const isRuntimeReady = (
    (surface.status === 'ready' && !surface.stale && currentSnapshot !== null)
    || (catalogProjection?.active_profile_id === null && catalogBindingStable)
  )
    && catalogSelectionAvailable;
  const snapshotChanged = Boolean(
    ceremonySnapshot && ceremonySnapshot !== currentBindingKey,
  );

  const mutationKeyForStep = (
    nextState: Extract<CeremonyState, 'resolving' | 'reviewing' | 'approving' | 'activating'>,
  ): string => {
    const step = nextState === 'resolving'
      ? 'resolve'
      : nextState === 'reviewing'
        ? 'review'
        : nextState === 'approving'
          ? 'approve'
          : 'activate';
    const identity = step === 'resolve'
      ? desiredPackIds.join(',')
      : step === 'activate'
        ? `${approval?.approval_id ?? ''}:${approval?.approval_digest ?? ''}`
        : `${candidate?.candidate_id ?? ''}:${candidate?.candidate_digest ?? ''}`;
    return `profile:${step}:${currentBindingKey}:${identity}`;
  };

  const beginStep = (nextState: Extract<CeremonyState, 'resolving' | 'reviewing' | 'approving' | 'activating'>) => {
    if (busyRef.current) return null;
    const mutationKey = mutationKeyForStep(nextState);
    const mutationMetadata = {
      kind: 'profile.ceremony',
      binding_key: currentBindingKey,
      step: nextState,
      profile_id: catalogEntry.profile_id,
      operation_id: profileOperationId(nextState),
      contract_id: PROFILE_CONTROL_CONTRACT,
      contract_map_digest: PINNED_FRONTEND_CONTRACT_MAP_ARTIFACT_DIGEST,
      ...(candidate ? {
        candidate_id: candidate.candidate_id,
        candidate_digest: candidate.candidate_digest,
      } : {}),
      ...(approval ? {
        approval_id: approval.approval_id,
        approval_digest: approval.approval_digest,
      } : {}),
    };
    let mutation: MutationJournalRecord;
    try {
      mutation = beginMutation(mutationKey, mutationMetadata);
    } catch (error) {
      if (error instanceof MutationBlockedError) {
        setUnknownMutation(error.record);
        setFailure({code: 'TIMEOUT', message: MUTATION_UNKNOWN_MESSAGE});
        setCeremonyState('result_unknown');
        return null;
      }
      throw error;
    }
    busyRef.current = true;
    const request = requestVersion.current + 1;
    requestVersion.current = request;
    const bindingKey = currentBindingRef.current;
    setFailure(null);
    setCeremonyState(nextState);
    setUnknownMutation(null);
    return {request, bindingKey, mutation, mutationKey};
  };

  const requestIsCurrent = (request: number, bindingKey: string): boolean => (
    requestVersion.current === request && currentBindingRef.current === bindingKey
  );

  const finishStep = (request: number): void => {
    if (requestVersion.current === request) busyRef.current = false;
  };

  const resetCeremony = () => {
    requestVersion.current += 1;
    busyRef.current = false;
    setCeremonyState('idle');
    setCandidate(null);
    setReviewed(null);
    setApproval(null);
    setCeremonySnapshot(null);
    setFailure(null);
    setUnknownMutation(null);
  };

  const selectPack = (pack: Pack) => {
    if (!pack.installed || !pack.approved || !pack.enabled || pack.required || ceremonyIsBusy) return;
    if (ceremonyState !== 'idle') resetCeremony();
    setSelectedPackIds((current) => current.includes(pack.id)
      ? current.filter((id) => id !== pack.id)
      : [...current, pack.id]);
  };

  const failClosed = (error: unknown) => {
    const code = classifyRuntimeSurfaceError(error);
    const message = error instanceof Error ? error.message : runtimeSurfaceErrorMessage(code);
    setFailure({code, message});
    setCeremonyState('error');
  };

  const validateStatusResult = (
    operation: {mutation: MutationJournalRecord},
    status: {state: string; result: unknown},
  ): ProfileResolveResult | ProfileReviewResult | ProfileApproveResult | ProfileActivateResult | null => {
    if (status.state !== 'succeeded') return null;
    const step = operation.mutation.metadata.step;
    if (step === 'resolving') return validateProfileResolveResult(status.result);
    if (step === 'reviewing') {
      const metadata = operation.mutation.metadata;
      const expectedCandidate = typeof metadata.candidate_id === 'string'
        && typeof metadata.candidate_digest === 'string'
        ? {candidate_id: metadata.candidate_id, candidate_digest: metadata.candidate_digest}
        : candidate
          ? {candidate_id: candidate.candidate_id, candidate_digest: candidate.candidate_digest}
          : undefined;
      return validateProfileReviewResult(status.result, expectedCandidate);
    }
    if (step === 'approving') return validateProfileApproveResult(status.result);
    if (step === 'activating') {
      const activation = validateProfileActivateResult(status.result);
      if (activation.profile_id !== authoritativeSelection.entry.profile_id) {
        throw new RuntimeSurfaceError(
          'DIGEST_MISMATCH',
          'Reconciled activation returned a different Profile than the selected catalog definition.',
        );
      }
      return activation;
    }
    throw new RuntimeSurfaceError('INVALID', 'Profile ceremony status step is invalid.');
  };

  const applyStatusResult = async (
    operation: {request: number; bindingKey: string; mutation: MutationJournalRecord},
    result: ProfileResolveResult | ProfileReviewResult | ProfileApproveResult | ProfileActivateResult,
  ): Promise<void> => {
    if (!requestIsCurrent(operation.request, operation.bindingKey)) return;
    if (result.state === 'resolved') {
      setCandidate(result);
      setReviewed(null);
      setApproval(null);
      setCeremonySnapshot(operation.bindingKey);
      setCeremonyState('resolved');
    } else if (result.state === 'reviewed') {
      setReviewed(result);
      setCeremonyState('reviewed');
    } else if (result.state === 'approved') {
      setApproval(result);
      setCeremonyState('approved');
    } else {
      setCeremonyState('active');
      broadcastRuntimeSurfaceRefresh();
      await onActivated?.(result);
    }
  };

  const handleMutationFailure = async (
    error: unknown,
    operation: {
      request: number;
      bindingKey: string;
      mutation: MutationJournalRecord;
      mutationKey: string;
    },
  ): Promise<void> => {
    if (isMutationResultUnknown(error)) {
      const unknown = markMutationUnknown(operation.mutationKey, operation.mutation.requestId);
      if (requestIsCurrent(operation.request, operation.bindingKey)) {
        setUnknownMutation(unknown);
        setFailure({code: 'TIMEOUT', message: MUTATION_UNKNOWN_MESSAGE});
        setCeremonyState('result_unknown');
      }
      let reconciled: Awaited<ReturnType<typeof reconcileMutationStatus>> | null = null;
      let reconciledResult: ProfileResolveResult | ProfileReviewResult | ProfileApproveResult | ProfileActivateResult | null = null;
      try {
        reconciled = await reconcileMutationStatus({
          record: unknown,
          binding: {
            requestId: unknown.requestId,
            operationId: profileOperationId(unknown.metadata.step),
            contractId: PROFILE_CONTROL_CONTRACT,
            mapArtifactDigest: PINNED_FRONTEND_CONTRACT_MAP_ARTIFACT_DIGEST,
          },
          refresh: async () => {
            await Promise.all([refreshMountedRuntimeSurfaces(), loadPacks()]);
          },
          verifySuccess: (status) => {
            reconciledResult = validateStatusResult(operation, status);
            return reconciledResult !== null;
          },
          isCurrent: () => requestIsCurrent(operation.request, operation.bindingKey),
        });
      } catch (error) {
        recordClientDiagnostic({
          code: 'profile.ceremony.reconciliation_failed',
          operation: 'profile.ceremony.hydrate',
          error,
        });
      }
      if (
        reconciled?.state === 'succeeded'
        && reconciledResult
        && requestIsCurrent(operation.request, operation.bindingKey)
      ) {
        setUnknownMutation(null);
        await applyStatusResult(operation, reconciledResult);
        setFailure(null);
        return;
      }
      if (reconciled?.state === 'failed' && requestIsCurrent(operation.request, operation.bindingKey)) {
        setUnknownMutation(null);
        setFailure({
          code: reconciled.status.safe_error_code === 'UNAPPROVED' ? 'APPROVAL_DENIED' : 'FAILED',
          message: reconciled.status.safe_error_code
            ? `The Host denied this Profile ceremony step (${reconciled.status.safe_error_code}).`
            : runtimeSurfaceErrorMessage('FAILED'),
        });
        setCeremonyState('error');
        return;
      }
      return;
    }
    completeMutation(operation.mutationKey, operation.mutation.requestId);
    if (requestIsCurrent(operation.request, operation.bindingKey)) failClosed(error);
  };

  // A fresh browsing context hydrates an unknown journal entry and immediately
  // asks the authenticated Host for its terminal outcome. No local projection
  // or persisted success flag can release this lock.
  useEffect(() => {
    if (!unknownMutation || ceremonyState !== 'result_unknown') return;
    const operation = {
      request: requestVersion.current,
      bindingKey: currentBindingKey,
      mutation: unknownMutation,
      mutationKey: unknownMutation.key,
    };
    let cancelled = false;
    let reconciledResult: ProfileResolveResult | ProfileReviewResult | ProfileApproveResult | ProfileActivateResult | null = null;
    void (async () => {
      try {
        const reconciled = await reconcileMutationStatus({
          record: unknownMutation,
          binding: {
            requestId: unknownMutation.requestId,
            operationId: profileOperationId(unknownMutation.metadata.step),
            contractId: PROFILE_CONTROL_CONTRACT,
            mapArtifactDigest: PINNED_FRONTEND_CONTRACT_MAP_ARTIFACT_DIGEST,
          },
          refresh: async () => {
            await Promise.all([refreshMountedRuntimeSurfaces(), loadPacks()]);
          },
          verifySuccess: (status) => {
            reconciledResult = validateStatusResult(operation, status);
            return reconciledResult !== null;
          },
          isCurrent: () => !cancelled && requestIsCurrent(operation.request, operation.bindingKey),
        });
        if (cancelled || !requestIsCurrent(operation.request, operation.bindingKey)) return;
        if (reconciled.state === 'succeeded' && reconciledResult) {
          setUnknownMutation(null);
          await applyStatusResult(operation, reconciledResult);
          setFailure(null);
        } else if (reconciled.state === 'failed') {
          setUnknownMutation(null);
          setFailure({
            code: reconciled.status.safe_error_code === 'UNAPPROVED' ? 'APPROVAL_DENIED' : 'FAILED',
            message: reconciled.status.safe_error_code
              ? `The Host denied this Profile ceremony step (${reconciled.status.safe_error_code}).`
              : runtimeSurfaceErrorMessage('FAILED'),
          });
          setCeremonyState('error');
        }
      } catch (error) {
        recordClientDiagnostic({
          code: 'profile.ceremony.reconciliation_failed',
          operation: 'profile.ceremony.unknown_result',
          error,
        });
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [unknownMutation, ceremonyState, currentBindingKey, loadPacks]);

  const refreshProfile = async () => {
    const original = unknownMutation;
    const operation = original
      ? {
        request: requestVersion.current,
        bindingKey: currentBindingKey,
        mutation: original,
        mutationKey: original.key,
      }
      : null;
    const reconcile = async (record: MutationJournalRecord) => {
      if (!operation) return null;
      let reconciledResult: ProfileResolveResult | ProfileReviewResult | ProfileApproveResult | ProfileActivateResult | null = null;
      try {
        const reconciled = await reconcileMutationStatus({
          record,
          binding: {
            requestId: record.requestId,
            operationId: profileOperationId(record.metadata.step),
            contractId: PROFILE_CONTROL_CONTRACT,
            mapArtifactDigest: PINNED_FRONTEND_CONTRACT_MAP_ARTIFACT_DIGEST,
          },
          refresh: async () => {
            await Promise.all([refreshMountedRuntimeSurfaces(), loadPacks()]);
          },
          verifySuccess: (status) => {
            reconciledResult = validateStatusResult(operation, status);
            return reconciledResult !== null;
          },
          isCurrent: () => requestIsCurrent(operation.request, operation.bindingKey),
        });
        if (reconciled.state === 'succeeded' && reconciledResult && requestIsCurrent(operation.request, operation.bindingKey)) {
          setUnknownMutation(null);
          await applyStatusResult(operation, reconciledResult);
          setFailure(null);
        } else if (reconciled.state === 'failed' && requestIsCurrent(operation.request, operation.bindingKey)) {
          setUnknownMutation(null);
          setFailure({
            code: reconciled.status.safe_error_code === 'UNAPPROVED' ? 'APPROVAL_DENIED' : 'FAILED',
            message: reconciled.status.safe_error_code
              ? `The Host denied this Profile ceremony step (${reconciled.status.safe_error_code}).`
              : runtimeSurfaceErrorMessage('FAILED'),
          });
          setCeremonyState('error');
        }
        return reconciled;
      } catch {
        return null;
      }
    };

    if (!operation) {
      await Promise.all([surface.refresh(true), loadPacks()]);
      return;
    }
    const first = await reconcile(original);
    if (first?.reconciled) return;
    await Promise.all([surface.refresh(true), loadPacks()]);
    const refreshed = listMutationJournal().find((record) => record.key === operation.mutationKey);
    if (refreshed?.state === 'unknown') await reconcile(refreshed);
  };

  const requireStableSnapshot = () => {
    if (!isRuntimeReady || snapshotChanged || !predecessorSnapshot) {
      throw new RuntimeSurfaceError('DIGEST_MISMATCH', runtimeSurfaceErrorMessage('DIGEST_MISMATCH'));
    }
    if (!catalogBindingStable) {
      throw new RuntimeSurfaceError(
        'DIGEST_MISMATCH',
        'The selected Profile definition or catalog lock changed. Refresh the authoritative catalog before continuing.',
      );
    }
    return predecessorSnapshot;
  };

  const resolve = async () => {
    const operation = beginStep('resolving');
    if (!operation) return;
    try {
      const snapshot = requireStableSnapshot();
      if (
        desiredPackIds.length === 0
        || new Set(desiredPackIds).size !== desiredPackIds.length
      ) {
        throw new RuntimeSurfaceError(
          'INVALID',
          'The selected authoritative Profile has no exact selectable Pack closure.',
        );
      }
      const input = {
        expected_profile_revision: snapshot.profile_revision,
        expected_plan_digest: snapshot.plan_digest,
        desired_pack_ids: [...desiredPackIds],
        profile_id: authoritativeSelection.entry.profile_id,
        profile_definition_digest: authoritativeSelection.entry.definition.digest,
        profile_catalog_digest: authoritativeSelection.catalogDigest,
        bundle_lock_digest: authoritativeSelection.bundleLockDigest,
      };
      const result = await client.resolve(input, operation.mutation.requestId);
      const binding = result.review.catalog_binding;
      if (
        !binding
        || typeof binding !== 'object'
        || Array.isArray(binding)
        || binding.profile_definition_digest !== authoritativeSelection.entry.definition.digest
        || binding.profile_catalog_digest !== authoritativeSelection.catalogDigest
        || binding.bundle_lock_digest !== authoritativeSelection.bundleLockDigest
      ) {
        throw new RuntimeSurfaceError(
          'DIGEST_MISMATCH',
          'The resolved candidate is not bound to the selected Profile definition and catalog lock.',
        );
      }
      if (result.review.profile && typeof result.review.profile === 'object' && !Array.isArray(result.review.profile)) {
        const resolvedProfileId = (result.review.profile as Record<string, unknown>).profile_id;
        if (resolvedProfileId !== authoritativeSelection.entry.profile_id) {
          throw new RuntimeSurfaceError('DIGEST_MISMATCH', 'The resolved candidate names a different Profile.');
        }
      }
      completeMutation(operation.mutationKey, operation.mutation.requestId);
      if (!requestIsCurrent(operation.request, operation.bindingKey)) return;
      setCandidate(result);
      setReviewed(null);
      setApproval(null);
      setCeremonySnapshot(operation.bindingKey);
      setCeremonyState('resolved');
    } catch (error) {
      await handleMutationFailure(error, operation);
    } finally {
      finishStep(operation.request);
    }
  };

  const review = async () => {
    const operation = beginStep('reviewing');
    if (!operation) return;
    try {
      requireStableSnapshot();
      if (!candidate) throw new RuntimeSurfaceError('INVALID', 'No resolved candidate is available.');
      const reviewInput = {candidate_id: candidate.candidate_id, candidate_digest: candidate.candidate_digest};
      const result = await client.review(reviewInput, operation.mutation.requestId);
      assertProfileCandidateMatches(reviewInput, result);
      completeMutation(operation.mutationKey, operation.mutation.requestId);
      if (!requestIsCurrent(operation.request, operation.bindingKey)) return;
      setReviewed(result);
      setCeremonyState('reviewed');
    } catch (error) {
      await handleMutationFailure(error, operation);
    } finally {
      finishStep(operation.request);
    }
  };

  const approve = async () => {
    const operation = beginStep('approving');
    if (!operation) return;
    try {
      requireStableSnapshot();
      if (!reviewed) throw new RuntimeSurfaceError('INVALID', 'Review must complete before approval.');
      if (!candidate) throw new RuntimeSurfaceError('INVALID', 'No resolved candidate is available.');
      assertProfileCandidateMatches(candidate, reviewed);
      const result = await client.approve({candidate_id: candidate.candidate_id, candidate_digest: candidate.candidate_digest}, operation.mutation.requestId);
      completeMutation(operation.mutationKey, operation.mutation.requestId);
      if (!requestIsCurrent(operation.request, operation.bindingKey)) return;
      setApproval(result);
      setCeremonyState('approved');
    } catch (error) {
      await handleMutationFailure(error, operation);
    } finally {
      finishStep(operation.request);
    }
  };

  const activate = async () => {
    const operation = beginStep('activating');
    if (!operation) return;
    try {
      requireStableSnapshot();
      if (!approval) throw new RuntimeSurfaceError('INVALID', 'Kernel approval is required before activation.');
      const result = await client.activate({approval_id: approval.approval_id, approval_digest: approval.approval_digest}, operation.mutation.requestId);
      if (result.profile_id !== authoritativeSelection.entry.profile_id) {
        throw new RuntimeSurfaceError('DIGEST_MISMATCH', 'Activation returned a different Profile than the selected catalog definition.');
      }
      completeMutation(operation.mutationKey, operation.mutation.requestId);
      if (!requestIsCurrent(operation.request, operation.bindingKey)) return;
      setCeremonyState('active');
      broadcastRuntimeSurfaceRefresh();
      await onActivated?.(result);
      await Promise.all([surface.refresh(true), loadPacks()]);
    } catch (error) {
      await handleMutationFailure(error, operation);
    } finally {
      finishStep(operation.request);
    }
  };

  const actionLabel = ceremonyState === 'resolved'
    ? 'Review exact candidate'
    : ceremonyState === 'reviewed'
      ? 'Request Kernel approval'
      : ceremonyState === 'approved'
        ? 'Activate approved Profile'
        : 'Resolve candidate';
  const action = ceremonyState === 'resolved' ? review : ceremonyState === 'reviewed' ? approve : ceremonyState === 'approved' ? activate : resolve;

  return (
    <Card id="profile-ceremony">
      <CardHeader>
        <div className="flex flex-wrap items-center justify-between gap-2">
          <CardTitle className="flex items-center gap-2"><LockKeyhole className="h-4 w-4" aria-hidden="true" />Runtime Profile change ceremony</CardTitle>
          <Badge variant={isRuntimeReady ? 'warning' : 'secondary'}>{isRuntimeReady ? 'digest-bound' : 'locked'}</Badge>
        </div>
        <CardDescription>The selected Profile is an authoritative definition. Inspect its exact closure and diff before each one-shot server-bound step. No client approval flag is accepted.</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-5">
        <div className="grid gap-2 sm:grid-cols-4" aria-label="Profile change steps">
          {(['resolve', 'review', 'approval', 'activation'] as const).map((step, index) => {
            const complete = (step === 'resolve' && ['resolved', 'reviewing', 'reviewed', 'approving', 'approved', 'activating', 'active'].includes(ceremonyState))
              || (step === 'review' && ['reviewed', 'approving', 'approved', 'activating', 'active'].includes(ceremonyState))
              || (step === 'approval' && ['approved', 'activating', 'active'].includes(ceremonyState))
              || (step === 'activation' && ceremonyState === 'active');
            return (
              <div key={step} className="flex min-h-11 items-center gap-2 rounded-lg border border-border bg-bg-main px-3 py-2 text-xs">
                {complete ? <CheckCircle2 className="h-4 w-4 shrink-0 text-emerald-600" aria-hidden="true" /> : <span className="flex h-4 w-4 shrink-0 items-center justify-center rounded-full border border-border text-xs">{index + 1}</span>}
                <span className={complete ? 'font-medium text-text-main' : 'text-text-muted'}>{step}</span>
                {index < 3 ? <ArrowRight className="ml-auto hidden h-3 w-3 text-text-muted sm:block" aria-hidden="true" /> : null}
              </div>
            );
          })}
        </div>

        <div>
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div>
              <h3 className="text-sm font-semibold text-text-main">Authoritative Pack closure editor</h3>
              <p className="mt-1 text-xs text-text-muted">
                Stage a content-addressed successor closure for {catalogEntry.display_name}. Base, Shell, Application, and dependency bindings stay authoritative; optional Pack additions and removals are reviewed under the same Profile identity.
              </p>
            </div>
            <Badge variant={selectedPackKey === (authoritativePackIds ?? []).slice().sort().join(',') ? 'outline' : 'warning'}>
              {selectedPackKey === (authoritativePackIds ?? []).slice().sort().join(',') ? 'Current closure' : 'Successor staged'}
            </Badge>
          </div>
          <div className="mt-3 rounded-lg border border-border bg-bg-main p-4">
            <dl className="grid gap-3 sm:grid-cols-2">
              <div><dt className="text-xs text-text-muted">Selected Profile</dt><dd className="mt-1 break-all font-mono text-xs text-text-main">{catalogEntry.profile_id}</dd></div>
              <div><dt className="text-xs text-text-muted">Definition digest</dt><dd className="mt-1 break-all font-mono text-xs text-text-main">{catalogEntry.definition.digest}</dd></div>
              <div><dt className="text-xs text-text-muted">Profile catalog digest</dt><dd className="mt-1 break-all font-mono text-xs text-text-main">{authoritativeSelection.catalogDigest}</dd></div>
              <div><dt className="text-xs text-text-muted">Bundle lock digest</dt><dd className="mt-1 break-all font-mono text-xs text-text-main">{authoritativeSelection.bundleLockDigest}</dd></div>
            </dl>
            <div className="mt-4" role="group" aria-label={`Edit Pack closure for ${catalogEntry.display_name}`}>
              <p className="text-xs font-medium text-text-main">Add Pack or remove an optional Pack</p>
              {packs.length === 0 ? (
                <p className="mt-3 rounded-lg border border-dashed border-border px-4 py-4 text-sm text-text-muted" role="status">No Pack catalog entries are available for this Profile closure.</p>
              ) : (
                <div className="mt-3 grid gap-2 sm:grid-cols-2">
                  {packs.map((pack) => {
                    const selected = selectedPackIds.includes(pack.id);
                    const eligible = pack.installed && pack.approved && pack.enabled && !pack.required;
                    const reason = pack.required
                      ? 'Required baseline'
                      : !pack.installed
                        ? 'Install required'
                        : !pack.approved
                          ? 'Authority approval required'
                          : !pack.enabled
                            ? 'Enable before adding'
                            : null;
                    return (
                      <button
                        aria-label={`${selected ? 'Remove' : 'Add'} Pack ${pack.name} ${selected ? 'from' : 'to'} ${catalogEntry.display_name} closure`}
                        aria-pressed={selected}
                        className="flex min-h-11 items-center gap-3 rounded-lg border border-border bg-bg-card px-3 py-2 text-left transition-colors hover:bg-bg-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--ring-color)] disabled:pointer-events-none disabled:opacity-60"
                        disabled={!eligible || ceremonyIsBusy}
                        key={pack.id}
                        onClick={() => selectPack(pack)}
                        type="button"
                      >
                        <span className={selected ? 'flex size-5 shrink-0 items-center justify-center rounded border border-accent bg-accent text-accent-fg' : 'size-5 shrink-0 rounded border border-border'} aria-hidden="true">
                          {selected ? <CheckCircle2 className="h-4 w-4" /> : null}
                        </span>
                        <span className="min-w-0 flex-1">
                          <span className="block truncate text-sm font-medium text-text-main">{selected ? 'Remove' : 'Add'} Pack · {pack.name}</span>
                          <span className="block truncate text-xs text-text-muted">{pack.id} · {pack.enabled ? 'enabled' : 'disabled'} · {pack.approved ? 'approved' : 'not approved'} · {pack.installed ? 'installed' : 'not installed'}</span>
                        </span>
                        {reason ? <span className="max-w-32 text-right text-xs text-text-muted">{reason}</span> : null}
                      </button>
                    );
                  })}
                </div>
              )}
            </div>
            {catalogMissingPackIds.length > 0 ? (
              <p className="mt-3 text-sm text-amber-700 dark:text-amber-300" role="alert">The current Pack catalog does not contain the exact requested entries: {catalogMissingPackIds.join(', ')}.</p>
            ) : null}
            {catalogIncompatiblePackIds.length > 0 ? (
              <p className="mt-3 text-sm text-amber-700 dark:text-amber-300" role="alert">One or more requested Packs are not installed, approved, enabled, or digest-matched. Refresh the Pack catalog or complete its separate lifecycle before continuing.</p>
            ) : null}
            {!catalogBindingStable ? (
              <div className="mt-3 flex items-start gap-2 text-sm text-amber-700 dark:text-amber-300" role="alert"><p className="min-w-0 flex-1">The authoritative Profile catalog is loading, stale, or no longer matches this selection. Ceremony actions are locked until it refreshes.</p></div>
            ) : null}
            {!catalogEntry.available ? (
              <div className="mt-3 flex items-start gap-2 text-sm text-destructive" role="alert">
                <AlertTriangle aria-hidden="true" className="mt-0.5 h-4 w-4 shrink-0" />
                <div className="min-w-0 flex-1">
                <p>This Profile is unavailable in the verified catalog.</p>
                <ul className="mt-1 list-disc pl-5">{catalogEntry.diagnostics.map((diagnostic) => <li key={`${diagnostic.code}:${diagnostic.subject}`}>{diagnostic.code}: {diagnostic.subject}</li>)}</ul>
                </div>
                <CopyErrorButton label="Copy unavailable Profile diagnostics" text={catalogEntry.diagnostics.map((diagnostic) => `${diagnostic.code}: ${diagnostic.subject}`).join('\n')} />
              </div>
            ) : null}
          </div>
        </div>

        {failure ? (
          <div className="flex items-start gap-3 rounded-lg border border-destructive/40 bg-destructive/5 px-4 py-3 text-sm" role="alert">
            <XCircle className="mt-0.5 h-4 w-4 shrink-0 text-destructive" aria-hidden="true" />
            <div className="min-w-0 flex-1"><p className="font-medium text-text-main">Profile ceremony stopped fail-closed</p><p className="mt-1 break-words text-text-muted">{failure.code}: {failure.message}</p></div>
            <CopyErrorButton label="Copy Profile ceremony error" text={`${failure.code}: ${failure.message}`} />
          </div>
        ) : null}

        {ceremonyState === 'result_unknown' && unknownMutation ? (
          <div className="flex flex-wrap items-start justify-between gap-3 rounded-lg border border-amber-300/70 bg-amber-50/70 px-4 py-3 text-sm dark:border-amber-800/60 dark:bg-amber-950/20" role="alert">
            <div className="min-w-0 flex-1">
              <p className="font-medium text-text-main">Profile ceremony result is unknown</p>
              <p className="mt-1 text-text-muted">{MUTATION_UNKNOWN_MESSAGE}</p>
              <p className="mt-1 break-all font-mono text-xs text-text-muted">Request identity: {unknownMutation.requestId}</p>
            </div>
            <Button
              type="button"
              variant="outline"
              onClick={() => void refreshProfile().catch((error) => {
                recordClientDiagnostic({
                  code: 'profile.ceremony.refresh_failed',
                  operation: 'profile.ceremony.refresh_authoritative_state',
                  error,
                });
              })}
            >
              Refresh authoritative state
            </Button>
          </div>
        ) : null}

        {candidate ? (
          <div className="rounded-lg border border-border bg-bg-main p-4">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <h3 className="text-sm font-semibold text-text-main">Exact candidate review</h3>
              <Badge variant={ceremonyState === 'active' ? 'success' : 'warning'}>{candidate.expires_in}s TTL</Badge>
            </div>
            <dl className="mt-3 grid gap-3 sm:grid-cols-2">
              <div><dt className="text-xs text-text-muted">Candidate digest</dt><dd className="mt-1 break-all font-mono text-xs text-text-main">{candidate.candidate_digest}</dd></div>
              <div><dt className="text-xs text-text-muted">Successor Profile revision</dt><dd className="mt-1 break-all font-mono text-xs text-text-main">{recordDigest(candidate.review.resolved_plan, ['profile_revision'])}</dd></div>
              <div><dt className="text-xs text-text-muted">ProfileLock digest</dt><dd className="mt-1 break-all font-mono text-xs text-text-main">{recordDigest(candidate.review.profile_lock, ['lock_digest'])}</dd></div>
              <div><dt className="text-xs text-text-muted">ResolvedPlan digest</dt><dd className="mt-1 break-all font-mono text-xs text-text-main">{recordDigest(candidate.review.resolved_plan, ['plan_digest'])}</dd></div>
              <div><dt className="text-xs text-text-muted">Predecessor Plan digest</dt><dd className="mt-1 break-all font-mono text-xs text-text-main">{recordDigest(candidate.review.predecessor, ['plan_digest'])}</dd></div>
            </dl>
            <p className="mt-3 text-xs text-text-muted">Write set: {candidate.write_set.length}. This immutable successor revision remains a candidate until the same Profile receives Authority approval and activation; no local diff is treated as authority.</p>
          </div>
        ) : null}

        {approval ? (
          <div className="rounded-lg border border-emerald-300/60 bg-emerald-50/60 p-4 dark:border-emerald-800/60 dark:bg-emerald-950/20">
            <div className="flex items-center gap-2 text-sm font-semibold text-text-main"><ShieldCheck className="h-4 w-4 text-emerald-600" aria-hidden="true" />Authority Kernel approval recorded</div>
            <p className="mt-2 break-all font-mono text-xs text-text-muted">{approval.authority_approval.approval_id} · {approval.authority_approval.approval_digest}</p>
            <p className="mt-1 text-xs text-text-muted">Decision: {approval.authority_approval.decision}; security epoch {approval.authority_approval.security_epoch}; TTL {approval.expires_in}s.</p>
          </div>
        ) : null}

        {snapshotChanged ? <p className="text-sm text-amber-700 dark:text-amber-300" role="alert">The accepted Profile snapshot changed. Refresh and resolve a new candidate.</p> : null}
        <Button
          type="button"
          className="min-h-11 self-start"
          onClick={() => void action()}
          loading={ceremonyIsBusy}
          disabled={!isRuntimeReady || snapshotChanged || desiredPackIds.length === 0 || ceremonyState === 'active' || ceremonyState === 'result_unknown'}
        >
          {actionLabel}
        </Button>
      </CardContent>
    </Card>
  );
}
