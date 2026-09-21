import {useCallback, useEffect, useRef, useState} from 'react';

import {
  classifyRuntimeSurfaceError,
  invokeRuntimeOperation,
  runtimeSurfaceErrorMessage,
  type RuntimeOperationDescriptor,
  type RuntimeSurfaceEnvelope,
  type RuntimeSurfaceErrorCode,
} from '@/src/lib/runtimeSurface';
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
import {refreshMountedRuntimeSurfaces} from '@/src/lib/runtimeSurfaceRefresh';
import {
  reconcileMutationStatus,
  type OperationStatus,
} from '@/src/lib/operationStatus';
import {PINNED_FRONTEND_CONTRACT_MAP_ARTIFACT_DIGEST} from '@/src/lib/generatedFrontendContractMap';

export type RuntimeInvocationState = 'idle' | 'running' | 'succeeded' | 'failed' | 'unknown';

export interface RuntimeInvocationError {
  code: RuntimeSurfaceErrorCode;
  message: string;
}

export function runtimeOperationIdentity(
  envelope: RuntimeSurfaceEnvelope<unknown>,
  operation: RuntimeOperationDescriptor,
): string {
  return [
    envelope.surface,
    envelope.profile_id,
    envelope.profile_revision,
    envelope.plan_digest,
    envelope.catalog_revision,
    envelope.records.activation_record.digest,
    operation.activation_id,
    operation.action,
    operation.operation_id,
    operation.contract_id,
    operation.owner_pack_id,
    operation.contribution_id,
    operation.artifact_digest,
    operation.invocation_contribution_id ?? '',
    operation.invocation_catalog_hash ?? '',
  ].join('\u0000');
}

interface ActiveInvocation {
  token: number;
  identity: string;
  envelope: RuntimeSurfaceEnvelope<unknown>;
}

export type RuntimeOperationInvoker = (request: {
  envelope: RuntimeSurfaceEnvelope<unknown>;
  operation: RuntimeOperationDescriptor;
  payload: Record<string, unknown>;
  requestId?: string;
}) => Promise<unknown>;

function stablePayload(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(stablePayload).join(',')}]`;
  if (value && typeof value === 'object') {
    return `{${Object.entries(value as Record<string, unknown>)
      .sort(([left], [right]) => left.localeCompare(right))
      .map(([key, item]) => `${JSON.stringify(key)}:${stablePayload(item)}`)
      .join(',')}}`;
  }
  return JSON.stringify(value) ?? String(value);
}

export function useRuntimeOperationInvocation(
  envelope: RuntimeSurfaceEnvelope<unknown> | null,
  operation: RuntimeOperationDescriptor | null,
  invokeOperation: RuntimeOperationInvoker = invokeRuntimeOperation,
) {
  const [state, setState] = useState<RuntimeInvocationState>('idle');
  const [error, setError] = useState<RuntimeInvocationError | null>(null);
  const identity = envelope && operation
    ? runtimeOperationIdentity(envelope, operation)
    : null;
  const nextToken = useRef(0);
  const active = useRef<ActiveInvocation | null>(null);
  const unknownMutationKey = useRef<string | null>(null);
  const binding = useRef<{envelope: RuntimeSurfaceEnvelope<unknown> | null; identity: string | null} | null>(null);
  const envelopeRef = useRef(envelope);
  const operationRef = useRef(operation);
  const identityRef = useRef(identity);
  envelopeRef.current = envelope;
  operationRef.current = operation;
  identityRef.current = identity;

  useEffect(() => {
    const currentUnknownKey = unknownMutationKey.current;
    const currentIdentityPrefix = `runtime:invoke:${identity ?? ''}:`;
    if (currentUnknownKey && !currentUnknownKey.startsWith(currentIdentityPrefix)) {
      // A completed or still-unknown request from the previous selection is
      // durable and can be recovered when that selection returns, but it must
      // never remain the active pointer for the newly selected operation.
      unknownMutationKey.current = null;
    }
    const changed = binding.current !== null
      && (binding.current.envelope !== envelope || binding.current.identity !== identity);
    binding.current = {envelope, identity};
    const current = active.current;
    if (changed || (current && (current.envelope !== envelope || current.identity !== identity))) {
      active.current = null;
      if (unknownMutationKey.current?.startsWith(`runtime:invoke:${identity ?? ''}:`)) {
        setState('unknown');
        setError({code: 'TIMEOUT', message: MUTATION_UNKNOWN_MESSAGE});
      } else {
        setState('idle');
        setError(null);
      }
    }
  }, [envelope, identity]);

  useEffect(() => {
    if (!identity || unknownMutationKey.current) return;
    const hydrated = listMutationJournal().find((record) => (
      record.state === 'unknown'
      && record.metadata.kind === 'runtime-operation-invocation'
      && record.key.startsWith(`runtime:invoke:${identity}:`)
    ));
    if (!hydrated) return;
    unknownMutationKey.current = hydrated.key;
    setState('unknown');
    setError({code: 'TIMEOUT', message: MUTATION_UNKNOWN_MESSAGE});
  }, [identity]);

  useEffect(() => () => {
    active.current = null;
  }, []);

  const reconcileRecord = useCallback(async (
    record: MutationJournalRecord,
    expectedIdentity: string,
    operationId: string,
    contractId: string,
    isCurrent: () => boolean,
  ): Promise<Awaited<ReturnType<typeof reconcileMutationStatus>> | null> => {
    let reconciled: Awaited<ReturnType<typeof reconcileMutationStatus>>;
    try {
      reconciled = await reconcileMutationStatus({
        record,
        binding: {
          requestId: record.requestId,
          operationId,
          contractId,
          mapArtifactDigest: PINNED_FRONTEND_CONTRACT_MAP_ARTIFACT_DIGEST,
        },
        refresh: () => refreshMountedRuntimeSurfaces(),
        verifySuccess: (_status: OperationStatus) => true,
        isCurrent,
      });
    } catch {
      // Unknown, tampered, cross-session, and stale status responses remain
      // journaled and blocked until a later authenticated reconciliation.
      return null;
    }
    if (reconciled.reconciled && unknownMutationKey.current === record.key) {
      // Terminal cleanup is bound to this exact durable request, not to the
      // currently selected operation. A newer selection must not inherit A's
      // result, but it must also not remain blocked by A's completed journal.
      unknownMutationKey.current = null;
    }
    if (!isCurrent() || identityRef.current !== expectedIdentity) return reconciled;
    if (reconciled.state === 'succeeded') {
      unknownMutationKey.current = null;
      setState('succeeded');
      setError(null);
    } else if (reconciled.state === 'failed') {
      unknownMutationKey.current = null;
      setState('failed');
      setError({
        code: 'FAILED',
        message: reconciled.status.safe_error_code
          ? `The Host denied this operation (${reconciled.status.safe_error_code}).`
          : runtimeSurfaceErrorMessage('FAILED'),
      });
    } else {
      setState('unknown');
      setError({code: 'TIMEOUT', message: MUTATION_UNKNOWN_MESSAGE});
    }
    return reconciled;
  }, []);

  const reconcileUnknown = useCallback(async () => {
    if (!identity || !unknownMutationKey.current || !operation) return null;
    const key = unknownMutationKey.current;
    if (!key.startsWith(`runtime:invoke:${identity}:`)) return null;
    const record = listMutationJournal().find((candidate) => (
      candidate.key === key
      && candidate.state === 'unknown'
      && candidate.metadata.kind === 'runtime-operation-invocation'
    ));
    if (!record) return null;
    const expectedIdentity = identity;
    return reconcileRecord(
      record,
      expectedIdentity,
      operation.operation_id,
      operation.contract_id,
      () => identityRef.current === expectedIdentity
        && unknownMutationKey.current === record.key,
    );
  }, [identity, operation?.contract_id, operation?.operation_id, reconcileRecord]);

  useEffect(() => {
    if (!identity || !unknownMutationKey.current || !operation) return;
    void reconcileUnknown();
  }, [identity, operation?.contract_id, operation?.operation_id, reconcileUnknown]);

  const invoke = useCallback(async (payload: Record<string, unknown>): Promise<void> => {
    if (!envelope || !operation || active.current || state === 'unknown') return;
    const invocationToken = nextToken.current + 1;
    nextToken.current = invocationToken;
    const invocationIdentity = runtimeOperationIdentity(envelope, operation);
    active.current = {
      token: invocationToken,
      identity: invocationIdentity,
      envelope,
    };
    const mutationKey = `runtime:invoke:${invocationIdentity}:${stablePayload(payload)}`;
    let mutation: MutationJournalRecord;
    try {
      mutation = beginMutation(mutationKey, {
        kind: 'runtime-operation-invocation',
        operation_id: operation.operation_id,
        contract_id: operation.contract_id,
        contract_map_digest: PINNED_FRONTEND_CONTRACT_MAP_ARTIFACT_DIGEST,
      });
    } catch (cause) {
      active.current = null;
      if (cause instanceof MutationBlockedError) {
        unknownMutationKey.current = mutationKey;
        setState('unknown');
        setError({code: 'TIMEOUT', message: MUTATION_UNKNOWN_MESSAGE});
        return;
      }
      setState('failed');
      setError({code: 'FAILED', message: runtimeSurfaceErrorMessage('FAILED')});
      return;
    }
    setState('running');
    setError(null);

    const isCurrent = (): boolean => {
      const current = active.current;
      return Boolean(
        current
        && current.token === invocationToken
        && current.identity === invocationIdentity
        && current.envelope === envelope
        && envelopeRef.current === envelope
        && operationRef.current !== null
        && runtimeOperationIdentity(envelopeRef.current, operationRef.current) === invocationIdentity,
      );
    };

    let resultUnknown = false;
    try {
      await invokeOperation({envelope, operation, payload, requestId: mutation.requestId});
      completeMutation(mutationKey, mutation.requestId);
      if (isCurrent()) {
        setState('succeeded');
      }
    } catch (cause) {
      if (isMutationResultUnknown(cause)) {
        resultUnknown = true;
        const unknown = markMutationUnknown(mutationKey, mutation.requestId);
        unknownMutationKey.current = mutationKey;
        setState('unknown');
        setError({code: 'TIMEOUT', message: MUTATION_UNKNOWN_MESSAGE});
        const reconciled = await reconcileRecord(
          unknown,
          invocationIdentity,
          operation.operation_id,
          operation.contract_id,
          isCurrent,
        );
        if (reconciled?.reconciled) {
          resultUnknown = false;
        }
      } else {
        completeMutation(mutationKey, mutation.requestId);
      }
      if (isCurrent()) {
        if (!isMutationResultUnknown(cause)) {
          const code = classifyRuntimeSurfaceError(cause);
          setState('failed');
          setError({code, message: runtimeSurfaceErrorMessage(code)});
        }
      }
    } finally {
      const current = active.current;
      if (current?.token === invocationToken) {
        const completedCurrent = isCurrent();
        active.current = null;
        const keepUnknownForCurrentBinding = resultUnknown
          && unknownMutationKey.current?.startsWith(`runtime:invoke:${identity ?? ''}:`);
        if (!completedCurrent && !keepUnknownForCurrentBinding) {
          setState('idle');
          setError(null);
        }
      }
    }
  }, [envelope, operation, invokeOperation, reconcileRecord, state]);

  return {
    state,
    error,
    busy: state === 'running' || state === 'unknown',
    invoke,
    reconcileUnknown,
  };
}
