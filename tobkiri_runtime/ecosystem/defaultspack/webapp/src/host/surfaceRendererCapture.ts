import type { FrontendCatalog, VerifiedFrontendContribution } from "./frontendContracts";
import { parseSurfaceRenderer, type SurfaceRenderer, type SurfaceTemplate } from "./surfaceTemplateContract";

export type SurfaceRendererCapture = {
  rendererContributionId: string; rendererOwnerPackId: string;
  rendererArtifactHash: string; rendererDescriptorHash: string;
  rendererBuildIdentity: string; rendererApiVersion: string;
  rendererImplementation: string; rendererExpiresAtMs: number;
};
export type ResolvedSurfaceRenderer = {
  item: VerifiedFrontendContribution; descriptor: SurfaceRenderer; capture: SurfaceRendererCapture;
};
const digest = (value: unknown) => typeof value === "string" && /^sha256:[0-9a-f]{64}$/.test(value);

/** Resolve only a Host-verified selected artifact to a finite shipped renderer.
 * Manifest fields select presentation; they cannot load code or grant authority.
 */
export function resolveSurfaceRenderer(
  catalog: FrontendCatalog, template: SurfaceTemplate, now = Date.now(),
): ResolvedSurfaceRenderer | null {
  if (catalog.version !== "rumi.ui.contribution.v1") return null;
  const candidates = catalog.contributions.filter((item) => item.contribution_id === template.renderer_contribution_id);
  if (candidates.length !== 1) return null;
  const item = candidates[0];
  if (item.kind !== "renderer" || item.mode !== "declarative" || item.renderer !== "tobkiri.ui.surface-renderer.v1"
    || item.resolved_profile_id !== catalog.profile_id || item.resolved_profile_revision !== catalog.profile_revision
    || item.resolved_activation_id !== catalog.activation_id || item.resolved_plan_hash !== catalog.plan_hash
    || catalog.quarantined_pack_ids.includes(item.owner_pack_id) || !digest(item.owner_pack_hash)
    || !digest(item.descriptor_hash) || !item.build_identity || item.module || item.isolated
    || !Number.isSafeInteger(item.resolved_expires_at_ms) || Number(item.resolved_expires_at_ms) <= now) return null;
  const descriptor = parseSurfaceRenderer(item.view);
  if (!descriptor || descriptor.api_version !== template.renderer_api_version
    || Number(item.resolved_expires_at_ms) > now + descriptor.ttl_ms
    || template.nodes.some((node) => !descriptor.patterns.includes(node.pattern))) return null;
  return { item, descriptor, capture: {
    rendererContributionId: item.contribution_id, rendererOwnerPackId: item.owner_pack_id,
    rendererArtifactHash: item.owner_pack_hash, rendererDescriptorHash: item.descriptor_hash,
    rendererBuildIdentity: item.build_identity, rendererApiVersion: descriptor.api_version,
    rendererImplementation: descriptor.implementation, rendererExpiresAtMs: Number(item.resolved_expires_at_ms),
  } };
}

/** Disable, update, expiry and rollback never silently rebind an open Surface. */
export function matchesSurfaceRendererCapture(
  catalog: FrontendCatalog, template: SurfaceTemplate, capture: SurfaceRendererCapture, now = Date.now(),
): boolean {
  const current = resolveSurfaceRenderer(catalog, template, now)?.capture;
  return !!current && Number.isSafeInteger(capture.rendererExpiresAtMs) && capture.rendererExpiresAtMs > now
    && Object.keys(current).every((key) => key === "rendererExpiresAtMs"
      ? capture.rendererExpiresAtMs <= current.rendererExpiresAtMs
      : current[key as keyof SurfaceRendererCapture] === capture[key as keyof SurfaceRendererCapture]);
}
