export interface ProfileLaunchFailure {
  message: string;
  recovery: 'packs' | 'profile' | null;
}

export function profileLaunchFailure(error: unknown): ProfileLaunchFailure {
  const raw = typeof error === 'string' ? error : error instanceof Error ? error.message : '';
  try {
    const parsed: unknown = JSON.parse(raw);
    if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
      const result = parsed as Record<string, unknown>;
      if (
        result.code === 'RUNTIME_BACKEND_UNAVAILABLE'
        && result.action === 'open_packs_to_prepare_packvm'
      ) {
        return {
          message: 'The required runtime backend is unavailable. Open Packs to check PackVM readiness, then try launching again.',
          recovery: 'packs',
        };
      }
      if (
        result.code === 'RUNTIME_PREPARATION_FAILED'
        && result.action === 'review_packs_and_retry'
      ) {
        return {
          message: 'The Launcher could not prepare the Defaultspack runtime. Open Packs to review its status, then retry Launch. If this continues, restart the Launcher.',
          recovery: 'packs',
        };
      }
      if (
        result.code === 'PROFILE_RERESOLUTION_REQUIRED'
        && result.action === 'reactivate_or_reresolve_profile'
      ) {
        return {
          message: 'The active Profile no longer authorizes its selected Shell. Review and reactivate this Profile before launching again.',
          recovery: 'profile',
        };
      }
    }
  } catch {
    // The Launcher deliberately redacts other launch failures.
  }
  return {
    message: 'The selected Shell could not be launched. Retry Launch; if it fails again, review the Profile and Pack status.',
    recovery: null,
  };
}

