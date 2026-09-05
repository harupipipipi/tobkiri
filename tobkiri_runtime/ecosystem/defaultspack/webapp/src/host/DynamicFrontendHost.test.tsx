import assert from "node:assert/strict";
import test from "node:test";
import { renderToStaticMarkup } from "react-dom/server";

import {
  DynamicFrontendHost,
  ISOLATED_FRONTEND_SANDBOX,
  ISOLATED_FRAME_RESPONSE_TARGET_ORIGIN,
  bindFrontendCapabilityClient,
  contributionsForRoute,
  frontendContributionRevisionKey,
  frontendActionErrorMessage,
  isolatedFrontendFrameUrl,
  parseIsolatedCapabilityRequest,
  quarantineFrontendContribution,
  resetFrontendHostQuarantineForTests,
  synchronizeFrontendHostQuarantine,
} from "./DynamicFrontendHost";
import type {
  CapturedCapabilityInvocation,
  FrontendCapabilityInvoker,
  FrontendCatalog,
  VerifiedFrontendContribution,
} from "./frontendContracts";

const contribution = (
  overrides: Partial<VerifiedFrontendContribution> = {},
): VerifiedFrontendContribution => ({
  contribution_id: "feature.route",
  kind: "route",
  mode: "declarative",
  label: "Feature",
  priority: 0,
  owner_pack_id: "feature-pack",
  owner_pack_hash: `sha256:${"1".repeat(64)}`,
  build_identity: "fixture",
  resolved_profile_id: "fixture",
  resolved_profile_revision: "r1",
  resolved_activation_id: "activation:fixture-1",
  resolved_plan_hash: "plan-1",
  descriptor_hash: `sha256:${"2".repeat(64)}`,
  route: "/feature",
  view: { title: "Dynamic feature" },
  localization: {},
  accessibility: { name: "Dynamic feature", keyboard: true },
  ...overrides,
});

const catalog = (
  items: VerifiedFrontendContribution[],
  overrides: Partial<FrontendCatalog> = {},
): FrontendCatalog => ({
  version: "rumi.ui.contribution.v1",
  profile_id: "fixture",
  profile_revision: "r1",
  activation_id: "activation:fixture-1",
  plan_hash: "plan-1",
  contributions: items,
  diagnostics: [],
  quarantined_pack_ids: [],
  catalog_hash: `sha256:${"3".repeat(64)}`,
  ...overrides,
});

const capabilities: FrontendCapabilityInvoker = {
  invokeAction: async () => ({ ok: true }),
  readDataSource: async () => ({ ok: true }),
};

test("route visibility follows the active resolved plan", () => {
  resetFrontendHostQuarantineForTests();
  const current = catalog([contribution()]);

  assert.equal(contributionsForRoute(current, "/feature", "plan-1").length, 1);
  assert.deepEqual(contributionsForRoute(current, "/feature", "plan-2"), []);
  assert.deepEqual(
    contributionsForRoute(
      catalog([contribution({ resolved_activation_id: "activation:stale" })]),
      "/feature",
      "plan-1",
    ),
    [],
  );
});

test("dynamic action and isolated data requests use Host-captured identity", async () => {
  const captured: CapturedCapabilityInvocation[] = [];
  const invoker: FrontendCapabilityInvoker = {
    invokeAction: async (request) => {
      captured.push(request);
      return { ok: true };
    },
    readDataSource: async (request) => {
      captured.push(request);
      return { ok: true };
    },
  };
  const item = contribution();
  const capturedCatalog = catalog([item]);
  const bound = bindFrontendCapabilityClient(capturedCatalog, item, invoker);
  const clientIdentityHints = {
    contributionId: "attacker.contribution",
    ownerPackId: "attacker-pack",
    planHash: "plan-attacker",
    catalogHash: "catalog-attacker",
  };

  await bound.invokeAction({
    contractId: "rumi.action.feature.run.v1",
    payload: { operation: "run", input: {} },
    ...clientIdentityHints,
  });
  await bound.readDataSource({
    contractId: "rumi.resource.feature.read.v1",
    payload: { operation: "read", input: { id: "feature" } },
    ...clientIdentityHints,
  });

  assert.deepEqual(captured, [
    {
      contractId: "rumi.action.feature.run.v1",
      payload: { operation: "run", input: {} },
      profileId: "fixture",
      profileRevision: "r1",
      activationId: "activation:fixture-1",
      planHash: "plan-1",
      catalogHash: capturedCatalog.catalog_hash,
      contributionId: "feature.route",
      ownerPackId: "feature-pack",
    },
    {
      contractId: "rumi.resource.feature.read.v1",
      payload: { operation: "read", input: { id: "feature" } },
      profileId: "fixture",
      profileRevision: "r1",
      activationId: "activation:fixture-1",
      planHash: "plan-1",
      catalogHash: capturedCatalog.catalog_hash,
      contributionId: "feature.route",
      ownerPackId: "feature-pack",
    },
  ]);
});

test("an activation A request is not rebound to activation B", async () => {
  const captured: CapturedCapabilityInvocation[] = [];
  const invoker: FrontendCapabilityInvoker = {
    invokeAction: async (request) => {
      captured.push(request);
      return { ok: true };
    },
    readDataSource: async () => ({ ok: true }),
  };
  const itemA = contribution();
  const catalogA = catalog([itemA]);
  const catalogB = catalog(
    [contribution({
      resolved_profile_revision: "r2",
      resolved_activation_id: "activation:fixture-2",
      resolved_plan_hash: "plan-2",
    })],
    {
      profile_revision: "r2",
      activation_id: "activation:fixture-2",
      plan_hash: "plan-2",
      catalog_hash: `sha256:${"4".repeat(64)}`,
    },
  );
  const boundA = bindFrontendCapabilityClient(catalogA, itemA, invoker);

  assert.notEqual(
    frontendContributionRevisionKey(itemA),
    frontendContributionRevisionKey(catalogB.contributions[0]),
  );

  await boundA.invokeAction({
    contractId: "rumi.action.feature.run.v1",
    payload: { operation: "run", input: {} },
    planHash: catalogB.plan_hash,
    catalogHash: catalogB.catalog_hash,
  });

  assert.equal(captured[0]?.activationId, "activation:fixture-1");
  assert.equal(captured[0]?.planHash, "plan-1");
  assert.equal(captured[0]?.catalogHash, catalogA.catalog_hash);
});

test("renders a declarative route without importing a product screen", () => {
  resetFrontendHostQuarantineForTests();
  const markup = renderToStaticMarkup(
    <DynamicFrontendHost
      catalog={catalog([contribution()])}
      route="/feature"
      activePlanHash="plan-1"
      capabilities={capabilities}
    />,
  );

  assert.match(markup, /<h2>Dynamic feature<\/h2>/);
  assert.doesNotMatch(markup, /iframe/);
});

test("missing pack contribution has a copyable isolated fallback", () => {
  resetFrontendHostQuarantineForTests();
  const markup = renderToStaticMarkup(
    <DynamicFrontendHost
      catalog={catalog([])}
      route="/feature"
      activePlanHash="plan-1"
      capabilities={capabilities}
    />,
  );

  assert.match(markup, /role="alert"/);
  assert.match(markup, /not available/);
  assert.match(markup, /data-error-icon="frontend-availability"/);
  assert.match(markup, /data-copy-icon=""/);
  assert.match(markup, /aria-label="Copy frontend availability error"/);
});

test("isolated contribution URLs are owner-bound and receive an opaque frame sandbox", () => {
  const isolated = contribution({
    mode: "isolated",
    isolated: {
      path: "/isolated/packs/feature-pack/index.html",
      rpc_contracts: ["rumi.resource.feature.read.v1"],
    },
  });

  assert.equal(ISOLATED_FRONTEND_SANDBOX, "allow-scripts");
  assert.equal(ISOLATED_FRAME_RESPONSE_TARGET_ORIGIN, "*");
  assert.equal(
    isolatedFrontendFrameUrl(
      isolated,
      "profile-1",
      "nonce-1",
      "https://tobkiri.local",
    ),
    "/isolated/packs/feature-pack/index.html?profile_id=profile-1#rumi_rpc_nonce=nonce-1",
  );
  assert.equal(
    isolatedFrontendFrameUrl(
      contribution({
        mode: "isolated",
        isolated: {
          path: "/isolated/packs/other-pack/index.html",
          rpc_contracts: [],
        },
      }),
      "profile-1",
      "nonce-1",
      "https://tobkiri.local",
    ),
    null,
  );
});

test("isolated frame RPC accepts only a bounded contract request envelope", () => {
  assert.deepEqual(
    parseIsolatedCapabilityRequest({
      type: "rumi.capability.request",
      requestId: "request-1",
      nonce: "nonce-1",
      contractId: "rumi.resource.feature.read.v1",
      payload: { operation: "read", input: { id: "feature" } },
    }),
    {
      requestId: "request-1",
      nonce: "nonce-1",
      contractId: "rumi.resource.feature.read.v1",
      payload: { operation: "read", input: { id: "feature" } },
    },
  );
  assert.equal(
    parseIsolatedCapabilityRequest({
      type: "rumi.capability.request",
      requestId: "request-1",
      nonce: "nonce-1",
      contractId: "rumi.resource.feature.read.v1",
      payload: { operation: "read", input: [] },
    }),
    null,
  );
});

test("catalog synchronization releases obsolete contribution quarantines", () => {
  resetFrontendHostQuarantineForTests();
  const failed = contribution();
  quarantineFrontendContribution(failed);
  assert.equal(contributionsForRoute(catalog([failed]), "/feature", "plan-1").length, 0);

  const replacement = contribution({
    descriptor_hash: `sha256:${"4".repeat(64)}`,
  });
  synchronizeFrontendHostQuarantine(catalog([replacement]));

  assert.equal(contributionsForRoute(catalog([failed]), "/feature", "plan-1").length, 1);
});

test("capability action errors preserve stale-catalog recovery guidance", () => {
  assert.equal(
    frontendActionErrorMessage({ code: "STALE_CATALOG" }),
    "This screen is out of date and is refreshing. Try the action again.",
  );
  assert.equal(
    frontendActionErrorMessage(new Error("Action denied")),
    "Action denied",
  );
});
