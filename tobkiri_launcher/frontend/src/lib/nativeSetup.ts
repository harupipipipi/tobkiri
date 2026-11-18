import {loadTauriInvoke} from './desktopHost';

export type DesktopSetupRoute = 'legacy' | 'native' | 'unavailable';

type NativeSetupReviewResult =
  | {readonly state: 'published' | 'verification_required' | 'unavailable'}
  | {readonly state: 'rejected'; readonly response: string};

export async function fetchDesktopSetupRoute(): Promise<DesktopSetupRoute> {
  try {
    const invoke = await loadTauriInvoke();
    const route = await invoke?.<unknown>('native_setup_route');
    return route === 'legacy' || route === 'native' ? route : 'unavailable';
  } catch {
    return 'unavailable';
  }
}

export async function startNativeSetupReview(): Promise<NativeSetupReviewResult> {
  const invoke = await loadTauriInvoke().catch((): null => null);
  if (!invoke) return {state: 'unavailable'};
  try {
    const result = await invoke<NativeSetupReviewResult>('start_native_setup_review');
    if (result?.state === 'published' || result?.state === 'unavailable'
      || (result?.state === 'rejected' && typeof result.response === 'string')) {
      return result;
    }
  } catch {
    // A lost IPC reply may follow publication. Only Host state can resolve it.
  }
  return {state: 'verification_required'};
}

export function nativeSetupRejectionMessage(response: string): string {
  if (response.length <= 16_384) {
    try {
      const error: unknown = JSON.parse(response)?.error;
      if (typeof error === 'string' && error.length <= 512 && error.trim()) return error;
    } catch {
      // An invalid diagnostic can still accompany an explicit rejection.
    }
  }
  return 'Harness Profile activation was rejected.';
}
