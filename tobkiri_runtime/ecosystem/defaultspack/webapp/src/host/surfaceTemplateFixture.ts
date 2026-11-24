import { readFileSync } from "node:fs";
import type { FrontendCatalog, VerifiedFrontendContribution } from "./frontendContracts";
import { viewReference, type CatalogView, type RegisteredCatalogView } from "./catalogViewRegistry";
import { SURFACE_EVENTS, type SurfaceRenderer, type SurfaceTemplate } from "./surfaceTemplateContract";

export const tenPatternTemplate = (): SurfaceTemplate => JSON.parse(readFileSync(
  new URL("../../../../../tests/fixtures/surface_templates_v1/ten_patterns.json", import.meta.url), "utf8",
)) as SurfaceTemplate;
const hash = (character: string) => `sha256:${character.repeat(64)}`;
export function surfaceFixture(implementation: SurfaceRenderer["implementation"] = "semantic_standard") {
  const template = tenPatternTemplate();
  const view: CatalogView = { version: "tobkiri.ui.view.v1", renderer: "surface_template", slot: "workspace_tab", surface_template: template };
  const item: VerifiedFrontendContribution = {
    contribution_id: "fixture.surface", kind: "view", mode: "declarative", label: "Semantic Surface", priority: 0,
    owner_pack_id: "fixture.surface-pack", owner_pack_hash: hash("a"), build_identity: "fixture-source",
    resolved_profile_id: "profile", resolved_profile_revision: hash("b"), resolved_activation_id: "activation",
    resolved_plan_hash: hash("c"), descriptor_hash: hash("d"), view, localization: {},
    accessibility: { name: "Semantic Surface", keyboard: true },
  };
  const renderer: VerifiedFrontendContribution = { ...item, kind: "renderer", contribution_id: "fixture.renderer",
    owner_pack_id: "fixture.renderer-pack", descriptor_hash: hash(implementation === "semantic_standard" ? "e" : "f"),
    renderer: "tobkiri.ui.surface-renderer.v1", resolved_expires_at_ms: Date.now() + 30000,
    view: { version: "tobkiri.ui.surface-renderer.v1", api_version: "1.0.0", implementation,
      patterns: Object.keys(SURFACE_EVENTS), ttl_ms: 300000 },
  };
  const action: VerifiedFrontendContribution = { ...item, kind: "action", contribution_id: "fixture.logic",
    owner_pack_id: "fixture.logic-pack", action_contract: "tobkiri.action.surface-example.v1", operation_id: "fixture.logic", view: null };
  const catalog: FrontendCatalog = { version: "rumi.ui.contribution.v1", profile_id: "profile", profile_revision: hash("b"),
    activation_id: "activation", plan_hash: hash("c"), selected_entry_route: "/chat", catalog_hash: hash("d"),
    contributions: [item, renderer, action], diagnostics: [], quarantined_pack_ids: [] };
  const registered: RegisteredCatalogView = { item, view, reference: viewReference(catalog, item) };
  return { template, catalog, registered, item, renderer, action };
}
