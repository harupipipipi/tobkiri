import {useCallback, useEffect, useRef, useState} from 'react';

import {
  classifyRuntimeSurfaceError,
  defaultRuntimeSurfaceClient,
  runtimeSurfaceErrorMessage,
  type RuntimeSurfaceClient,
  type RuntimeSurfaceEnvelope,
  type RuntimeSurfaceErrorCode,
  type RuntimeSurfaceId,
} from '@/src/lib/runtimeSurface';
import {registerRuntimeSurfaceRefresher} from '@/src/lib/runtimeSurfaceRefresh';

export type {RuntimeSurfaceClient} from '@/src/lib/runtimeSurface';

export type RuntimeSurfaceLoadStatus =
  | 'idle'
  | 'loading'
  | 'ready'
  | 'unavailable'
  | 'profile_not_active'
  | 'stale'
  | 'timeout'
  | 'digest_mismatch'
  | 'approval_denied'
  | 'error';

export interface RuntimeSurfaceLoadError {
  code: RuntimeSurfaceErrorCode;
  message: string;
}

export interface RuntimeSurfaceState<T> {
  data: RuntimeSurfaceEnvelope<T> | null;
  status: RuntimeSurfaceLoadStatus;
  error: RuntimeSurfaceLoadError | null;
  stale: boolean;
  canMutate: false;
  refresh: (force?: boolean) => Promise<void>;
}

export const RUNTIME_SURFACE_REFRESH_EVENT = 'tobkiri:runtime-surface-refresh';

/** Ask every mounted runtime-surface hook to re-read its captured v4 snapshot. */
export function broadcastRuntimeSurfaceRefresh(): void {
  if (typeof window !== 'undefined') {
    window.dispatchEvent(new window.Event(RUNTIME_SURFACE_REFRESH_EVENT));
  }
}

function statusForError(code: RuntimeSurfaceErrorCode): RuntimeSurfaceLoadStatus {
  if (code === 'UNAVAILABLE') return 'unavailable';
  if (code === 'PROFILE_NOT_ACTIVE') return 'profile_not_active';
  if (code === 'TIMEOUT') return 'timeout';
  if (code === 'STALE') return 'stale';
  if (code === 'DIGEST_MISMATCH') return 'digest_mismatch';
  if (code === 'APPROVAL_DENIED') return 'approval_denied';
  return 'error';
}

export function useRuntimeSurface<T>(
  surface: RuntimeSurfaceId,
  client: RuntimeSurfaceClient = defaultRuntimeSurfaceClient,
): RuntimeSurfaceState<T> {
  const [state, setState] = useState<Omit<RuntimeSurfaceState<T>, 'refresh'>>({
    data: null,
    status: 'idle',
    error: null,
    stale: false,
    canMutate: false,
  });
  const requestVersion = useRef(0);
  const acceptedSnapshot = useRef<RuntimeSurfaceEnvelope<T> | null>(null);

  const refresh = useCallback(async (force = false) => {
    const currentRequest = requestVersion.current + 1;
    requestVersion.current = currentRequest;
    if (force) acceptedSnapshot.current = null;
    setState((current) => ({
      ...current,
      status: 'loading',
      error: null,
    }));
    try {
      const previous = force ? null : acceptedSnapshot.current;
      const next = await client.read<T>(surface, previous ? {
        expected_profile_revision: previous.profile_revision,
        expected_plan_digest: previous.plan_digest,
      } : undefined);
      if (requestVersion.current !== currentRequest) return;
      setState({
        data: next,
        status: 'ready',
        error: null,
        stale: false,
        canMutate: false,
      });
      acceptedSnapshot.current = next;
    } catch (error) {
      if (requestVersion.current !== currentRequest) return;
      const code = classifyRuntimeSurfaceError(error);
      if (code === 'STALE' || code === 'DIGEST_MISMATCH') {
        // The captured guard is no longer authoritative. Keep the accepted
        // envelope visible and read-only, but let the next explicit retry
        // perform an unguarded authoritative read.
        acceptedSnapshot.current = null;
      }
      setState((current) => ({
        ...current,
        status: statusForError(code),
        error: {code, message: runtimeSurfaceErrorMessage(code)},
        stale: Boolean(current.data),
        canMutate: false,
      }));
    }
  }, [client, surface]);

  const refreshRef = useRef(refresh);
  refreshRef.current = refresh;

  useEffect(() => {
    void refresh();
    return () => {
      requestVersion.current += 1;
    };
  }, [refresh]);

  useEffect(() => {
    const handleRefresh = () => {
      void refresh(true);
    };
    window.addEventListener(RUNTIME_SURFACE_REFRESH_EVENT, handleRefresh);
    return () => window.removeEventListener(RUNTIME_SURFACE_REFRESH_EVENT, handleRefresh);
  }, [refresh]);

  useEffect(() => registerRuntimeSurfaceRefresher(() => refreshRef.current(true)), []);

  return {...state, refresh};
}
