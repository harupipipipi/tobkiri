/** Non-secret Host capture fields partition presentation caches and navigation. */
export type SearchCaptureIdentity = {
  profile_id: string;
  profile_revision: string;
  activation_id: string;
  plan_hash: string;
  catalog_hash: string;
  security_epoch?: number | string;
};

/** Keep caller constraints while fencing a new capture of the same Profile. */
export function searchCaptureScope(
  scopeId: string,
  capture?: SearchCaptureIdentity | null,
  activePlanHash?: string,
): string {
  if (!capture) return JSON.stringify([scopeId]);
  const epoch = capture.security_epoch;
  const securityEpoch = typeof epoch === "string"
    || (typeof epoch === "number" && Number.isFinite(epoch)) ? epoch : null;
  return JSON.stringify([
    scopeId,
    capture.profile_id,
    capture.profile_revision,
    capture.activation_id,
    capture.plan_hash,
    capture.catalog_hash,
    activePlanHash ?? capture.plan_hash,
    securityEpoch,
  ]);
}
