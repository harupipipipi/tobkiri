import assert from "node:assert/strict";
import test from "node:test";
import { resolveProfileScreenRequest } from "../src/host/HostBootstrap";
import { frontendHostFixtureCatalog, frontendHostFixtureScreenPath } from "./frontendHostFixture";

test("the alternate approval test Profile retains complete matching Application captures", () => {
  for (const profileId of ["defaults", "approval-other"] as const) {
    const catalog = frontendHostFixtureCatalog(true, profileId);
    assert.equal(catalog.profile_id, profileId);
    assert.ok(catalog.contributions.length > 0);
    for (const contribution of catalog.contributions) {
      assert.equal(contribution.resolved_profile_id, profileId);
      assert.equal(contribution.resolved_profile_revision, catalog.profile_revision);
      assert.equal(contribution.resolved_activation_id, catalog.activation_id);
      assert.equal(contribution.resolved_plan_hash, catalog.plan_hash);
    }
    assert.deepEqual(resolveProfileScreenRequest(frontendHostFixtureScreenPath("/chat", profileId), catalog), { kind: "route", route: "/chat" });
    const other = profileId === "defaults" ? "approval-other" : "defaults";
    assert.equal(resolveProfileScreenRequest(frontendHostFixtureScreenPath("/chat", other), catalog).kind, "reject");
  }
});
