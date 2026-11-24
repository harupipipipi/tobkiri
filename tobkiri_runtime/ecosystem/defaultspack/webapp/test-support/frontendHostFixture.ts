import frontendContractMap from "../../defaultspack/frontend_contract_map.v4.json" with { type: "json" };
import type { FrontendCatalog, VerifiedFrontendContribution } from "../src/host/frontendContracts";
import { profileScreenPath } from "../src/lib/profileRoute";

export type FrontendHostFixtureProfile = "defaults" | "approval-other";

/** Use the same persistent Profile identity for the fixture URL and catalog. */
export function frontendHostFixtureScreenPath(applicationRoute: string, profileId: FrontendHostFixtureProfile = "defaults"): string {
  return profileScreenPath(profileId, applicationRoute);
}

/** Model the Host capture without adding compatibility routes or bypassing admission. */
export function frontendHostFixtureCatalog(applicationChat = false, profileId: FrontendHostFixtureProfile = "defaults"): FrontendCatalog {
  const profileRevision = "e2e-profile-revision";
  const activationId = "e2e-activation";
  const planHash = `sha256:${"b".repeat(64)}`;
  const capture = {
    kind: "route" as const,
    description: "Start a conversation with Tobkiri.",
    priority: 0,
    owner_pack_id: "defaultspack",
    owner_pack_hash: `sha256:${"c".repeat(64)}`,
    build_identity: "defaultspack.conversation",
    resolved_profile_id: profileId,
    resolved_profile_revision: profileRevision,
    resolved_activation_id: activationId,
    resolved_plan_hash: planHash,
    descriptor_hash: `sha256:${"d".repeat(64)}`,
    localization: {},
    accessibility: { name: "Tobkiri Conversation", keyboard: true },
  };
  const contributions: VerifiedFrontendContribution[] = applicationChat
    ? frontendContractMap.frontend.entries
      .filter((entry) => entry.implementation === "defaultspack.chat")
      .map((entry) => ({
        ...capture,
        contribution_id: entry.contribution_id,
        mode: "application_builtin",
        implementation: entry.implementation,
        label: entry.label,
        route: entry.route,
        route_match: "exact",
      }))
    : [{
      ...capture,
      contribution_id: "defaults.conversation.complete",
      mode: "declarative",
      label: "Tobkiri Conversation",
      route: "/chat",
      action_contract: "conversation.turn.v1",
      view: { type: "conversation_v4" },
    }];
  return {
    version: "rumi.ui.contribution.v1",
    profile_id: profileId,
    profile_revision: profileRevision,
    activation_id: activationId,
    plan_hash: planHash,
    selected_entry_route: "/chat",
    contributions,
    diagnostics: [],
    quarantined_pack_ids: [],
    catalog_hash: `sha256:${"e".repeat(64)}`,
  };
}
