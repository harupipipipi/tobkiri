import {useEffect, useRef} from 'react';
import {recordClientDiagnostic} from '@/src/lib/clientDiagnostics';

const REFRESH_INTERVAL_MS = 30_000;
const RETRY_INTERVAL_MS = 5_000;
const MAX_RETRY_INTERVAL_MS = 60_000;

/**
 * Refresh a read-only surface while visible, retrying failures with backoff.
 * Callers must ignore results when the signal is aborted. Mutations are never
 * passed to this hook; pausing also invalidates any read that preceded a write.
 */
export function useAutoRefresh(
  refresh: (signal: AbortSignal) => Promise<boolean>,
  {enabled = true, paused = false}: {enabled?: boolean; paused?: boolean} = {},
): void {
  const refreshRef = useRef(refresh);
  refreshRef.current = refresh;

  useEffect(() => {
    if (!enabled || paused) return;
    let stopped = false;
    let running = false;
    let failures = 0;
    let timer: number | undefined;
    const controller = new AbortController();

    const canRefresh = () => document.visibilityState !== 'hidden'
      && navigator.onLine !== false;
    const clearTimer = () => {
      window.clearTimeout(timer);
      timer = undefined;
    };
    const run = async () => {
      if (stopped || running || !canRefresh()) return;
      clearTimer();
      running = true;
      try {
        const success = await refreshRef.current(controller.signal);
        failures = success ? 0 : Math.min(failures + 1, 5);
      } catch (error) {
        failures = Math.min(failures + 1, 5);
        recordClientDiagnostic({code: 'ui.auto_refresh.failed', operation: 'surface.refresh', error});
      } finally {
        running = false;
        if (!stopped && canRefresh()) {
          const delay = failures === 0
            ? REFRESH_INTERVAL_MS
            : Math.min(RETRY_INTERVAL_MS * 2 ** (failures - 1), MAX_RETRY_INTERVAL_MS);
          timer = window.setTimeout(() => void run(), delay);
        }
      }
    };
    const resume = () => {
      if (canRefresh()) void run();
      else clearTimer();
    };

    document.addEventListener('visibilitychange', resume);
    window.addEventListener('focus', resume);
    window.addEventListener('online', resume);
    window.addEventListener('offline', resume);
    void run();
    return () => {
      stopped = true;
      controller.abort();
      clearTimer();
      document.removeEventListener('visibilitychange', resume);
      window.removeEventListener('focus', resume);
      window.removeEventListener('online', resume);
      window.removeEventListener('offline', resume);
    };
  }, [enabled, paused]);
}
