import assert from "node:assert/strict";
import test from "node:test";
import { renderToStaticMarkup } from "react-dom/server";

import {
  DynamicFrontendHost,
  ISOLATED_FRONTEND_SANDBOX,
  ISOLATED_FRAME_RESPONSE_TARGET_ORIGIN,
  bindFrontendCapabilityClient,
  buildFrontendComponentRegistry,
  contributionsForRoute,
  frontendContributionRevisionKey,
  frontendActionErrorMessage,
  isolatedFrontendFrameUrl,
  parseIsolatedCapabilityRequest,
  quarantineFrontendContribution,
  resetFrontendHostQuarantineForTests,
  synchronizeFrontendHostQuarantine,
} from "./DynamicFrontendHost";
import {
  FRONTEND_COMPONENT_API_VERSION,
  UNSUPPORTED_COMPONENT_ID,
} from "./frontendComponentRegistry";
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
  selected_entry_route: "/chat",
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

test("the selected full Chat implementation is independent of Profile name", () => {
  resetFrontendHostQuarantineForTests();
  const item = contribution({
    contribution_id: "defaultspack.frontend.chat",
    mode: "application_builtin",
    implementation: "defaultspack.chat",
    owner_pack_id: "defaultspack",
    build_identity: "runtime.tauri.application.default",
    route: "/chat",
  });
  const render = (value: FrontendCatalog, plan = "plan-1") => renderToStaticMarkup(
    <DynamicFrontendHost catalog={value} route="/chat" activePlanHash={plan} capabilities={capabilities} />,
  );
  for (const profileId of ["defaults", "research", "renamed-profile"]) {
    const selected = { ...item, resolved_profile_id: profileId };
    const current = catalog([selected], { profile_id: profileId });
    assert.match(render(current), /data-application-implementation="defaultspack.chat"/);
    assert.doesNotMatch(render(current), /Message Tobkiri/);
    assert.doesNotMatch(render(current, "plan-stale"), /data-application-implementation/);
    for (const overrides of [
      { resolved_profile_id: "other" },
      { resolved_profile_revision: "stale" },
      { resolved_activation_id: "activation:stale" },
      { resolved_plan_hash: "plan-stale" },
      { owner_pack_id: "other" },
      { build_identity: "" },
      { owner_pack_hash: "invalid" },
      { descriptor_hash: "invalid" },
      { implementation: "unselected.javascript" },
    ]) {
      assert.doesNotMatch(render(catalog([{ ...selected, ...overrides }], {
        profile_id: profileId,
      })), /data-application-implementation/);
    }
    assert.doesNotMatch(render({ ...current, quarantined_pack_ids: ["defaultspack"] }), /data-application-implementation/);
    quarantineFrontendContribution(selected);
    assert.doesNotMatch(render(current), /data-application-implementation/);
    resetFrontendHostQuarantineForTests();
  }
});

test("ambiguous routes fail closed and subpath entries require a path boundary", () => {
  const share = contribution({ route: "/share", route_match: "subpath" });
  assert.equal(contributionsForRoute(catalog([share]), "/share/record-1", "plan-1").length, 1);
  for (const route of ["/shared/record", "/share/../chat", "/share/%2e%2e/chat", "/share?chat=1", "/share//record", "/share/record/"]) {
    assert.deepEqual(contributionsForRoute(catalog([share]), route, "plan-1"), []);
  }
  assert.deepEqual(contributionsForRoute(catalog([share, { ...share, contribution_id: "other.share" }]), "/share", "plan-1"), []);
  assert.deepEqual(contributionsForRoute(catalog([contribution({ route: "/", route_match: "subpath" })]), "/unknown", "plan-1"), []);
});

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

test("renders a registry-backed generic component without a product import", () => {
  resetFrontendHostQuarantineForTests();
  const item = contribution({
    view: {
      type: "component",
      component_id: "rumi.ui.status_surface",
      api_version: FRONTEND_COMPONENT_API_VERSION,
      slot: "route",
      props: { title: "Pack-owned workflow", body: "Ready", tone: "success" },
    },
  });

  const markup = renderToStaticMarkup(
    <DynamicFrontendHost
      catalog={catalog([item])}
      route="/feature"
      activePlanHash="plan-1"
      capabilities={capabilities}
    />,
  );

  assert.match(markup, /data-frontend-component-id="rumi.ui.status_surface"/);
  assert.match(markup, /Pack-owned workflow/);
  assert.match(markup, /data-status-tone="success"/);
});

test("component registry fails closed for unknown ids, slots, props, and contracts", () => {
  const registry = buildFrontendComponentRegistry(catalog([]));
  const base = {
    componentId: "rumi.ui.status_surface",
    apiVersion: FRONTEND_COMPONENT_API_VERSION,
    slot: "route",
    props: { title: "Ready" },
  };

  assert.equal(registry.resolve({ ...base, componentId: "pack.ui.unknown" }).diagnostic?.code, "frontend_component_unknown");
  assert.equal(registry.resolve({ ...base, apiVersion: "rumi.frontend.component.v2" }).diagnostic?.code, "frontend_component_version_mismatch");
  assert.equal(registry.resolve({ ...base, slot: "secret_overlay" }).diagnostic?.code, "frontend_component_slot_mismatch");
  assert.equal(registry.resolve({ ...base, props: { title: "Ready", secret: "no" } }).diagnostic?.code, "frontend_component_props_invalid");
  assert.equal(registry.resolve({ ...base, actionContract: "rumi.action.secret.read.v1" }).diagnostic?.code, "frontend_component_action_contract_mismatch");
  assert.equal(registry.resolve({ ...base, dataContract: "rumi.resource.secret.read.v1" }).diagnostic?.code, "frontend_component_data_contract_mismatch");
});

test("unknown component ids render a visible deterministic fallback", () => {
  const item = contribution({
    view: {
      type: "component",
      component_id: "pack.ui.missing",
      api_version: FRONTEND_COMPONENT_API_VERSION,
      slot: "route",
      props: {},
    },
  });

  const markup = renderToStaticMarkup(
    <DynamicFrontendHost
      catalog={catalog([item])}
      route="/feature"
      activePlanHash="plan-1"
      capabilities={capabilities}
    />,
  );

  assert.match(markup, /data-frontend-component-fallback="true"/);
  assert.match(markup, /data-component-diagnostic="frontend_component_unknown"/);
  assert.match(markup, /Unsupported component/);
});

test("verified pack component registration follows catalog replacement and detects collisions", () => {
  const packComponent = contribution({
    contribution_id: "pack-a.component.card",
    kind: "component",
    mode: "same_origin_builtin",
    route: null,
    view: null,
    component_id: "pack.ui.card",
    api_version: FRONTEND_COMPONENT_API_VERSION,
    supported_slots: ["workspace"],
    props_schema: {
      type: "object",
      required: ["title"],
      additionalProperties: false,
      properties: { title: { type: "string" } },
    },
    fallback_component_id: UNSUPPORTED_COMPONENT_ID,
    module: {
      path: "/static/packs/feature-pack/component.js",
      export: "Component",
      content_hash: `sha256:${"4".repeat(64)}`,
    },
  });
  const binding = {
    componentId: "pack.ui.card",
    apiVersion: FRONTEND_COMPONENT_API_VERSION,
    slot: "workspace",
    props: { title: "Card" },
  };
  const withPack = buildFrontendComponentRegistry(catalog([packComponent]));
  const withoutPack = buildFrontendComponentRegistry(catalog([]));

  assert.equal(withPack.resolve(binding).registration.ownerPackId, "feature-pack");
  assert.equal(withoutPack.resolve(binding).diagnostic?.code, "frontend_component_unknown");

  const collision = contribution({
    ...packComponent,
    contribution_id: "pack-b.component.card",
    owner_pack_id: "pack-b",
    module: {
      ...packComponent.module!,
      path: "/static/packs/pack-b/component.js",
    },
  });
  const collided = buildFrontendComponentRegistry(catalog([packComponent, collision]));
  assert.equal(collided.diagnostics()[0]?.code, "frontend_component_collision");
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
