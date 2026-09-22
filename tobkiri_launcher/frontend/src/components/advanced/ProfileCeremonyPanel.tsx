import {useEffect, useMemo, useRef, useState} from 'react';
import {ArrowRight, CheckCircle2, LockKeyhole, ShieldCheck, XCircle} from 'lucide-react';

import {Badge} from '@/src/components/ui/Badge';
import {Button} from '@/src/components/ui/Button';
import {Card, CardContent, CardDescription, CardHeader, CardTitle} from '@/src/components/ui/Card';
import {broadcastRuntimeSurfaceRefresh, type RuntimeSurfaceState} from '@/src/hooks/useRuntimeSurface';
import {
  classifyRuntimeSurfaceError,
  extractExactProfileCatalogSelectablePackIds,
  extractExactProfileSelectablePackIds,
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
  packsLoading,
  loadPacks,
  client = defaultProfileCeremonyClient,
  onActivated,
  authoritativeSelection,
  catalogSurface,
  onBusyChange,
}: {
  surface: RuntimeSurfaceState<unknown>;
  packs: Pack[];
  packsLoading: boolean;
  loadPacks: () => Promise<void>;
  client?: ProfileCeremonyClient;
  onActivated?: (result: ProfileActivateResult) => Promise<void>;
  authoritativeSelection?: {
    entry: RuntimeProfileCatalogEntry;
    catalogDigest: string;
    bundleLockDigest: string;
  };
  catalogSurface?: RuntimeSurfaceState<RuntimeProfileCatalogProjection>;
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
  const initialized = useRef(false);
  const requestVersion = useRef(0);
  const busyRef = useRef(false);

  const isCatalogMode = Boolean(authoritativeSelection);
  const authoritativePackIds = useMemo(
    () => authoritativeSelection
      ? extractExactProfileCatalogSelectablePackIds(authoritativeSelection.entry)
      : null,
    [authoritativeSelection],
  );

  const closureIds = useMemo(
    () => surface.data ? extractExactProfileSelectablePackIds(surface.data.data) : null,
    [surface.data],
  );
  const profileBoundPacks = surface.data
    ? packs.filter((pack) => (
      pack.profileRevision === surface.data?.profile_revision
      && pack.planDigest === surface.data?.plan_digest
    ))
    : [];
  const selectablePacks = profileBoundPacks.filter((pack) => pack.installed && pack.approved && pack.enabled);
  const selectablePackIds = new Set(selectablePacks.map((pack) => pack.id));
  const controlCatalogRevisions = new Set(packs.map((pack) => pack.catalogRevision).filter(Boolean));
  const packCatalogBound = Boolean(
    surface.data
    && packs.length > 0
    && controlCatalogRevisions.size === 1
    && profileBoundPacks.length === packs.length,
  );
  const profilePackIdsAvailable = closureIds !== null;
  const missingClosureIds = (closureIds ?? []).filter((id) => !packs.some((pack) => pack.id === id));

  const catalogProjection = catalogSurface?.data?.data ?? null;
  const catalogEntry = authoritativeSelection?.entry;
  const catalogBindingStable = Boolean(
    isCatalogMode
    && catalogSurface
    && catalogSurface.status === 'ready'
    && !catalogSurface.stale
    && catalogSurface.data
    && catalogProjection
    && catalogProjection.catalog_digest === authoritativeSelection?.catalogDigest
    && catalogProjection.bundle_lock_digest === authoritativeSelection?.bundleLockDigest
    && catalogProjection.profiles.some((entry) => (
      entry.profile_id === catalogEntry?.profile_id
      && entry.definition.digest === catalogEntry?.definition.digest
    )),
  );
  const catalogPackRows = (authoritativePackIds ?? []).map((id) => packs.find((pack) => pack.id === id) ?? null);
  const catalogMissingPackIds = catalogPackRows.flatMap((pack, index) => pack ? [] : [authoritativePackIds?.[index] ?? '']);
  const catalogIncompatiblePackIds = catalogPackRows.flatMap((pack, index) => {
    if (!pack) return [];
    const closureEntry = catalogEntry?.pack_closure.find((item) => item.pack_id === pack.id);
    if (
      !closureEntry
      || pack.artifactDigest !== closureEntry.artifact_digest
      || !pack.installed
      || !pack.approved
      || !pack.enabled
    ) return [authoritativePackIds?.[index] ?? pack.id];
    return [];
  });

  useEffect(() => {
    if (!isCatalogMode) return;
    requestVersion.current += 1;
    busyRef.current = false;
    setCeremonyState('idle');
    setCandidate(null);
    setReviewed(null);
    setApproval(null);
    setCeremonySnapshot(null);
    setFailure(null);
    setUnknownMutation(null);
  }, [isCatalogMode, catalogEntry?.profile_id, catalogEntry?.definition.digest, authoritativeSelection?.catalogDigest, authoritativeSelection?.bundleLockDigest]);

  useEffect(() => {
    const busy = ['resolving', 'reviewing', 'approving', 'activating'].includes(ceremonyState);
    onBusyChange?.(busy);
    return () => {
      onBusyChange?.(false);
    };
  }, [ceremonyState, onBusyChange]);

  useEffect(() => {
    if (initialized.current) return;
    if (isCatalogMode) return;
    const initial = closureIds && closureIds.length > 0
      ? closureIds.filter((id) => selectablePacks.some((pack) => pack.id === id))
      : selectablePacks.filter((pack) => pack.required || pack.enabled).map((pack) => pack.id);
    if (initial.length > 0) {
      initialized.current = true;
      setSelectedPackIds(initial);
    }
  }, [closureIds, selectablePacks, isCatalogMode]);

  const currentSnapshot = snapshotForProfileCeremony(surface.data);
  const ceremonyIsBusy = ['resolving', 'reviewing', 'approving', 'activating'].includes(ceremonyState);
  const desiredPackIds = isCatalogMode ? (authoritativePackIds ?? []) : selectedPackIds;
  const currentBindingKey = [
    currentSnapshot ? snapshotKey(currentSnapshot) : 'no-runtime-snapshot',
    isCatalogMode ? catalogEntry?.profile_id ?? 'no-profile-selection' : 'defaults-pack-set',
    isCatalogMode ? catalogEntry?.definition.digest ?? 'no-profile-definition' : '',
    isCatalogMode ? authoritativeSelection?.catalogDigest ?? 'no-profile-catalog' : '',
    isCatalogMode ? authoritativeSelection?.bundleLockDigest ?? 'no-bundle-lock' : '',
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
    catalogEntry
    && catalogEntry.available
    && authoritativePackIds
    && authoritativePackIds.length > 0
    && catalogBindingStable
    && catalogMissingPackIds.length === 0
    && catalogIncompatiblePackIds.length === 0,
  );
  const isRuntimeReady = surface.status === 'ready'
    && !surface.stale
    && currentSnapshot !== null
    && (isCatalogMode
      ? catalogSelectionAvailable
      : profilePackIdsAvailable && packCatalogBound && missingClosureIds.length === 0);
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
      profile_id: currentSnapshot?.profile_id ?? '',
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
    if (isCatalogMode || !pack.installed || !pack.approved || pack.required || ceremonyIsBusy) return;
    if (ceremonyState !== 'idle') resetCeremony();
    setSelectedPackIds((current) => {
      const next = current.includes(pack.id)
        ? current.filter((id) => id !== pack.id)
        : [...current, pack.id];
      return next;
    });
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
      if (isCatalogMode && activation.profile_id !== authoritativeSelection?.entry.profile_id) {
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
    if (!isRuntimeReady || snapshotChanged || !currentSnapshot) {
      throw new RuntimeSurfaceError('DIGEST_MISMATCH', runtimeSurfaceErrorMessage('DIGEST_MISMATCH'));
    }
    if (isCatalogMode && (!catalogEntry || !catalogBindingStable)) {
      throw new RuntimeSurfaceError(
        'DIGEST_MISMATCH',
        'The selected Profile definition or catalog lock changed. Refresh the authoritative catalog before continuing.',
      );
    }
    return currentSnapshot;
  };

  const resolve = async () => {
    const operation = beginStep('resolving');
    if (!operation) return;
    try {
      const snapshot = requireStableSnapshot();
      if (
        desiredPackIds.length === 0
        || new Set(desiredPackIds).size !== desiredPackIds.length
        || (!isCatalogMode && desiredPackIds.some((id) => !selectablePackIds.has(id)))
      ) {
        throw new RuntimeSurfaceError(
          'INVALID',
          isCatalogMode
            ? 'The selected authoritative Profile has no exact selectable Pack closure.'
            : 'Select at least one approved Pack before resolving a candidate.',
        );
      }
      const input = {
        profile_id: snapshot.profile_id,
        expected_profile_revision: snapshot.profile_revision,
        expected_plan_digest: snapshot.plan_digest,
        desired_pack_ids: [...desiredPackIds],
        ...(isCatalogMode && authoritativeSelection
          ? {
            profile_id: authoritativeSelection.entry.profile_id,
            profile_definition_digest: authoritativeSelection.entry.definition.digest,
            profile_catalog_digest: authoritativeSelection.catalogDigest,
            bundle_lock_digest: authoritativeSelection.bundleLockDigest,
          }
          : {}),
      };
      const result = await client.resolve(input, operation.mutation.requestId);
      if (isCatalogMode) {
        const binding = result.review.catalog_binding;
        if (
          !binding
          || typeof binding !== 'object'
          || Array.isArray(binding)
          || binding.profile_definition_digest !== authoritativeSelection?.entry.definition.digest
          || binding.profile_catalog_digest !== authoritativeSelection?.catalogDigest
          || binding.bundle_lock_digest !== authoritativeSelection?.bundleLockDigest
        ) {
          throw new RuntimeSurfaceError(
            'DIGEST_MISMATCH',
            'The resolved candidate is not bound to the selected Profile definition and catalog lock.',
          );
        }
        if (result.review.profile && typeof result.review.profile === 'object' && !Array.isArray(result.review.profile)) {
          const resolvedProfileId = (result.review.profile as Record<string, unknown>).profile_id;
          if (resolvedProfileId !== authoritativeSelection?.entry.profile_id) {
            throw new RuntimeSurfaceError('DIGEST_MISMATCH', 'The resolved candidate names a different Profile.');
          }
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
      if (isCatalogMode && result.profile_id !== authoritativeSelection?.entry.profile_id) {
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
    <Card>
      <CardHeader>
        <div className="flex flex-wrap items-center justify-between gap-2">
          <CardTitle className="flex items-center gap-2"><LockKeyhole className="h-4 w-4" aria-hidden="true" />Runtime Profile change ceremony</CardTitle>
          <Badge variant={isRuntimeReady ? 'warning' : 'secondary'}>{isRuntimeReady ? 'digest-bound' : 'locked'}</Badge>
        </div>
        <CardDescription>{isCatalogMode
          ? 'The selected Profile is an authoritative definition. Inspect its exact closure and diff before each one-shot server-bound step. No client approval flag is accepted.'
          : 'Edit the separate Defaults Pack set, then inspect the exact diff before each one-shot server-bound step. No client approval flag is accepted.'}</CardDescription>
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

        {isCatalogMode ? (
          <div>
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div>
                <h3 className="text-sm font-semibold text-text-main">Authoritative Pack closure</h3>
                <p className="mt-1 text-xs text-text-muted">The selected Profile definition owns this exact closure. Pack rows are used only to report compatibility; they cannot add or remove Packs from a named Profile.</p>
              </div>
              <Badge variant="outline">{desiredPackIds.length} requested</Badge>
            </div>
            {catalogEntry ? (
              <div className="mt-3 rounded-lg border border-border bg-bg-main p-4">
                <dl className="grid gap-3 sm:grid-cols-2">
                  <div><dt className="text-xs text-text-muted">Selected Profile</dt><dd className="mt-1 break-all font-mono text-xs text-text-main">{catalogEntry.profile_id}</dd></div>
                  <div><dt className="text-xs text-text-muted">Definition digest</dt><dd className="mt-1 break-all font-mono text-xs text-text-main">{catalogEntry.definition.digest}</dd></div>
                  <div><dt className="text-xs text-text-muted">Profile catalog digest</dt><dd className="mt-1 break-all font-mono text-xs text-text-main">{authoritativeSelection?.catalogDigest}</dd></div>
                  <div><dt className="text-xs text-text-muted">Bundle lock digest</dt><dd className="mt-1 break-all font-mono text-xs text-text-main">{authoritativeSelection?.bundleLockDigest}</dd></div>
                </dl>
                {catalogMissingPackIds.length > 0 ? (
                  <p className="mt-3 text-sm text-amber-700 dark:text-amber-300" role="alert">The current Pack catalog does not contain the exact requested entries: {catalogMissingPackIds.join(', ')}.</p>
                ) : null}
                {catalogIncompatiblePackIds.length > 0 ? (
                  <p className="mt-3 text-sm text-amber-700 dark:text-amber-300" role="alert">One or more requested Packs are not installed, approved, enabled, or digest-matched. Refresh the Pack catalog or complete its separate lifecycle before continuing.</p>
                ) : null}
                {!catalogBindingStable ? (
                  <p className="mt-3 text-sm text-amber-700 dark:text-amber-300" role="alert">The authoritative Profile catalog is loading, stale, or no longer matches this selection. Ceremony actions are locked until it refreshes.</p>
                ) : null}
                {!catalogEntry.available ? (
                  <div className="mt-3 text-sm text-destructive" role="alert">
                    <p>This Profile is unavailable in the verified catalog.</p>
                    <ul className="mt-1 list-disc pl-5">{catalogEntry.diagnostics.map((diagnostic) => <li key={`${diagnostic.code}:${diagnostic.subject}`}>{diagnostic.code}: {diagnostic.subject}</li>)}</ul>
                  </div>
                ) : null}
              </div>
            ) : (
              <p className="mt-3 rounded-lg border border-dashed border-border px-4 py-4 text-sm text-text-muted">No authoritative Profile definition is selected.</p>
            )}
          </div>
        ) : (
          <div>
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div>
                <h3 className="text-sm font-semibold text-text-main">Defaults Pack-set editor</h3>
                <p className="mt-1 text-xs text-text-muted">This separate lifecycle edits the Defaults Pack set. Named Profile selection is always read-only from the authoritative catalog above.</p>
              </div>
              <Badge variant="outline">{selectedPackIds.length} selected</Badge>
            </div>
            {packsLoading && packs.length === 0 ? (
              <div className="mt-3 h-24 animate-pulse rounded-lg border border-border bg-bg-main" role="status">Loading approved Packs…</div>
            ) : packs.length === 0 ? (
              <p className="mt-3 rounded-lg border border-dashed border-border px-4 py-4 text-sm text-text-muted">No Pack control catalog entries are available.</p>
            ) : (
              <div className="mt-3 grid gap-2 sm:grid-cols-2">
                {packs.map((pack) => {
                  const eligible = selectablePackIds.has(pack.id) && packCatalogBound;
                  const checked = selectedPackIds.includes(pack.id);
                  const inSnapshot = profileBoundPacks.some((candidate) => candidate.id === pack.id);
                  const reason = !inSnapshot
                    ? 'Profile revision or Plan digest is stale'
                    : !pack.installed
                      ? 'Install required'
                      : !pack.approved
                        ? 'Kernel Pack approval required'
                        : !pack.enabled
                          ? 'Enable the Pack before adding it to the closure'
                          : !packCatalogBound
                            ? 'Pack catalog is not bound to the accepted snapshot'
                            : null;
                  return (
                    <button
                      key={pack.id}
                      type="button"
                      className="flex min-h-11 items-center gap-3 rounded-lg border border-border bg-bg-main px-3 py-2 text-left transition-colors hover:bg-bg-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--ring-color)] disabled:pointer-events-none disabled:opacity-60"
                      aria-pressed={checked}
                      aria-label={`Toggle Defaults Pack ${pack.name}`}
                      disabled={!eligible || pack.required || ceremonyIsBusy}
                      onClick={() => selectPack(pack)}
                    >
                      <span className={checked ? 'flex size-5 shrink-0 items-center justify-center rounded border border-accent bg-accent text-accent-fg' : 'size-5 shrink-0 rounded border border-border'} aria-hidden="true">
                        {checked ? <CheckCircle2 className="h-4 w-4" /> : null}
                      </span>
                      <span className="min-w-0 flex-1">
                        <span className="block truncate text-sm font-medium text-text-main">{pack.name}</span>
                        <span className="block truncate text-xs text-text-muted">{pack.id} · {pack.enabled ? 'enabled' : 'disabled'} · {pack.approved ? 'approved' : 'not approved'} · {pack.installed ? 'installed' : 'not installed'}</span>
                      </span>
                      {pack.required ? <Badge variant="secondary">Required</Badge> : null}
                      {!pack.required && reason ? <span className="max-w-40 text-right text-xs text-text-muted">{reason}</span> : null}
                    </button>
                  );
                })}
              </div>
            )}
            {missingClosureIds.length > 0 ? (
              <p className="mt-3 text-sm text-amber-700 dark:text-amber-300" role="alert">Active closure entries missing from the canonical Pack catalog: {missingClosureIds.join(', ')}. Refresh before changing the Defaults Pack set.</p>
            ) : null}
            {surface.data && !packCatalogBound && packs.length > 0 ? (
              <p className="mt-3 text-sm text-amber-700 dark:text-amber-300" role="alert">Pack lifecycle rows are not bound to the accepted Profile revision and Plan digest, or the control catalog revision is inconsistent. Candidate actions are locked until both views refresh.</p>
            ) : null}
            {surface.data && !profilePackIdsAvailable ? (
              <p className="mt-3 text-sm text-amber-700 dark:text-amber-300" role="alert">The canonical Profile document did not publish a valid provider Pack set. Candidate selection is locked.</p>
            ) : null}
          </div>
        )}

        {failure ? (
          <div className="flex items-start gap-3 rounded-lg border border-destructive/40 bg-destructive/5 px-4 py-3 text-sm" role="alert">
            <XCircle className="mt-0.5 h-4 w-4 shrink-0 text-destructive" aria-hidden="true" />
            <div><p className="font-medium text-text-main">Profile ceremony stopped fail-closed</p><p className="mt-1 text-text-muted">{failure.code}: {failure.message}</p></div>
          </div>
        ) : null}

        {ceremonyState === 'result_unknown' && unknownMutation ? (
          <div className="flex flex-wrap items-start justify-between gap-3 rounded-lg border border-amber-300/70 bg-amber-50/70 px-4 py-3 text-sm dark:border-amber-800/60 dark:bg-amber-950/20" role="alert">
            <div>
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
              <div><dt className="text-xs text-text-muted">ProfileLock digest</dt><dd className="mt-1 break-all font-mono text-xs text-text-main">{recordDigest(candidate.review.profile_lock, ['lock_digest'])}</dd></div>
              <div><dt className="text-xs text-text-muted">ResolvedPlan digest</dt><dd className="mt-1 break-all font-mono text-xs text-text-main">{recordDigest(candidate.review.resolved_plan, ['plan_digest'])}</dd></div>
              <div><dt className="text-xs text-text-muted">Predecessor Plan digest</dt><dd className="mt-1 break-all font-mono text-xs text-text-main">{recordDigest(candidate.review.predecessor, ['plan_digest'])}</dd></div>
            </dl>
            <p className="mt-3 text-xs text-text-muted">Write set: {candidate.write_set.length}. Review shows canonical records and digests; no local diff is treated as authority.</p>
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
