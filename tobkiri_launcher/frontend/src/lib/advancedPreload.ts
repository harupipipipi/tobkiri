import type {
  CapabilityProfilesResponseData,
  StartupProfilesResponseData,
} from './apiTypes';
import { prefetchApiGet } from './api';

let advancedPreloadPromise: Promise<void> | null = null;

function capabilityProfileId(
  startupData: StartupProfilesResponseData,
  capabilityData: CapabilityProfilesResponseData,
): string {
  const startupProfile = startupData.profiles.find(
    (profile) => profile.profile_id === startupData.active_profile_id,
  ) ?? startupData.profiles[0];
  const candidates = [
    startupProfile?.capability_profile_id,
    startupProfile?.default_graph,
    startupProfile?.graph_id,
  ].filter((value): value is string => Boolean(value));
  return candidates.find((candidate) => capabilityData.profiles.some(
    (profile) => profile.profile_id === candidate,
  )) ?? capabilityData.profiles[0]?.profile_id ?? '';
}

/** Starts Advanced data requests in parallel and never participates in Home readiness. */
export function preloadAdvancedPanelData(): Promise<void> {
  if (advancedPreloadPromise) return advancedPreloadPromise;

  const startupRequest = prefetchApiGet<StartupProfilesResponseData>('/api/panel/startup/profiles');
  const capabilityRequest = prefetchApiGet<CapabilityProfilesResponseData>('/api/panel/profiles');
  const profileNodesRequest = Promise.all([startupRequest, capabilityRequest]).then(
    ([startupData, capabilityData]) => {
      const profileId = capabilityProfileId(startupData, capabilityData);
      return profileId
        ? prefetchApiGet(`/api/panel/profiles/${encodeURIComponent(profileId)}/nodes`)
        : undefined;
    },
  );

  advancedPreloadPromise = Promise.allSettled([
    startupRequest,
    capabilityRequest,
    profileNodesRequest,
    prefetchApiGet('/api/panel/flows'),
    prefetchApiGet('/api/panel/packs'),
    prefetchApiGet('/api/panel/graphs'),
    prefetchApiGet('/api/panel/api-map'),
    prefetchApiGet('/api/panel/settings/profile'),
    prefetchApiGet('/api/panel/updates/settings'),
  ]).then(() => undefined);

  return advancedPreloadPromise;
}
