import { render, screen } from "@testing-library/react";
import { beforeEach, expect, test, vi } from "vitest";

import {
  DynamicFrontendHost,
  contributionsForRoute,
  resetFrontendHostQuarantineForTests,
} from "./DynamicFrontendHost";
import type {
  FrontendCapabilityClient,
  FrontendCatalog,
  VerifiedFrontendContribution,
} from "./frontendContracts";

const contribution = (overrides: Partial<VerifiedFrontendContribution> = {}): VerifiedFrontendContribution => ({
  contribution_id: "feature.route",
  kind: "route",
  mode: "declarative",
  label: "Feature",
  priority: 0,
  owner_pack_id: "feature-pack",
  owner_pack_hash: `sha256:${"1".repeat(64)}`,
  build_identity: "fixture",
  resolved_profile_revision: "r1",
  resolved_plan_hash: "plan-1",
  descriptor_hash: `sha256:${"2".repeat(64)}`,
  route: "/feature",
  view: { title: "Dynamic feature" },
  localization: {},
  accessibility: { name: "Dynamic feature", keyboard: true },
  ...overrides,
});

const catalog = (items: VerifiedFrontendContribution[]): FrontendCatalog => ({
  version: "rumi.ui.contribution.v1",
  profile_id: "fixture",
  profile_revision: "r1",
  plan_hash: "plan-1",
  contributions: items,
  diagnostics: [],
  quarantined_pack_ids: [],
  catalog_hash: `sha256:${"3".repeat(64)}`,
});

const capabilities: FrontendCapabilityClient = {
  invokeAction: vi.fn(async () => ({ ok: true })),
  readDataSource: vi.fn(async () => ({ ok: true })),
};

beforeEach(() => resetFrontendHostQuarantineForTests());

test("route visibility follows the active resolved plan", () => {
  const current = catalog([contribution()]);

  expect(contributionsForRoute(current, "/feature", "plan-1")).toHaveLength(1);
  expect(contributionsForRoute(current, "/feature", "plan-2")).toEqual([]);
});

test("renders a declarative route without importing a product screen", () => {
  render(
    <DynamicFrontendHost
      catalog={catalog([contribution()])}
      route="/feature"
      activePlanHash="plan-1"
      capabilities={capabilities}
    />,
  );

  expect(screen.getByRole("heading", { name: "Dynamic feature" })).toBeTruthy();
});

test("missing pack contribution has a generic isolated fallback", () => {
  render(
    <DynamicFrontendHost
      catalog={catalog([])}
      route="/feature"
      activePlanHash="plan-1"
      capabilities={capabilities}
    />,
  );

  expect(screen.getByRole("status").textContent).toContain("not available");
});

test("isolated executable UI fails closed until a dedicated origin is available", () => {
  render(
    <DynamicFrontendHost
      catalog={catalog([contribution({
        mode: "isolated",
        isolated: {
          path: "/isolated/packs/feature-pack/index.html",
          rpc_contracts: ["rumi.action.feature.open.v1"],
        },
      })])}
      route="/feature"
      activePlanHash="plan-1"
      capabilities={capabilities}
    />,
  );

  expect(screen.queryByTitle("Dynamic feature")).toBeNull();
  expect(screen.getByRole("status").textContent).toContain("dedicated isolated origin");
});
