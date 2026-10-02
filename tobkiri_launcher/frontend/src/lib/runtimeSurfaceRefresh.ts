export type RuntimeSurfaceRefresher = () => Promise<void>;

const mountedRefreshers = new Set<RuntimeSurfaceRefresher>();

export function registerRuntimeSurfaceRefresher(
  refresher: RuntimeSurfaceRefresher,
): () => void {
  mountedRefreshers.add(refresher);
  return () => {
    mountedRefreshers.delete(refresher);
  };
}

/** Refresh every mounted canonical runtime projection and wait for completion. */
export async function refreshMountedRuntimeSurfaces(): Promise<void> {
  await Promise.all([...mountedRefreshers].map((refresher) => refresher()));
}
