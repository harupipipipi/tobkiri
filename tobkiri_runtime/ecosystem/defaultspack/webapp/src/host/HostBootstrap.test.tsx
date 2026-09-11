import assert from "node:assert/strict";
import test from "node:test";

import {
  FrontendCapabilityError,
  fetchDynamicCatalog,
  invokeCapability,
  resolveProfileScreenRequest,
} from "./HostBootstrap";
import type {
  CapturedCapabilityInvocation,
  FrontendCatalog,
} from "./frontendContracts";

const catalog: FrontendCatalog = {
  version: "rumi.ui.contribution.v1",
  profile_id: "defaults",
  profile_revision: "profile-1",
  activation_id: "activation:defaults-1",
  plan_hash: "plan-1",
  selected_entry_route: "/chat",
  contributions: [],
  diagnostics: [],
  quarantined_pack_ids: [],
  catalog_hash: `sha256:${"1".repeat(64)}`,
};

const invocation = (
  overrides: Partial<CapturedCapabilityInvocation> = {},
): CapturedCapabilityInvocation => ({
  contractId: "conversation.turn.v1",
  payload: { messages: [{ role: "user", content: "Hello" }] },
  profileId: catalog.profile_id,
  profileRevision: catalog.profile_revision,
  activationId: catalog.activation_id,
  contributionId: "defaults.conversation.complete",
  ownerPackId: "defaultspack",
  planHash: catalog.plan_hash,
  catalogHash: catalog.catalog_hash,
  ...overrides,
});

test("screen route resolution binds URL identity and declared non-Chat entries", () => {
  const codingCatalog: FrontendCatalog = {
    ...catalog,
    profile_id: "coding-profile",
    selected_entry_route: "/coding",
    contributions: [{
      contribution_id: "coding.frontend",
      kind: "route",
      mode: "declarative",
      label: "Coding",
      priority: 0,
      owner_pack_id: "codingpack",
      owner_pack_hash: `sha256:${"2".repeat(64)}`,
      build_identity: "coding.fixture",
      resolved_profile_id: "coding-profile",
      resolved_profile_revision: catalog.profile_revision,
      resolved_activation_id: catalog.activation_id,
      resolved_plan_hash: catalog.plan_hash,
      descriptor_hash: `sha256:${"3".repeat(64)}`,
      route: "/coding",
      view: { title: "Coding" },
      localization: {},
      accessibility: { name: "Coding", keyboard: true },
    }],
  };

  assert.deepEqual(resolveProfileScreenRequest("/p/coding-profile", codingCatalog), {
    kind: "redirect",
    destination: "/p/coding-profile/coding",
  });
  assert.equal(
    resolveProfileScreenRequest("/p/coding-profile", {
      ...codingCatalog,
      selected_entry_route: "/missing",
    }).kind,
    "reject",
  );
  assert.deepEqual(resolveProfileScreenRequest("/p/coding-profile/coding", codingCatalog), {
    kind: "route",
    route: "/coding",
  });
  assert.equal(
    resolveProfileScreenRequest("/p/defaults/coding", codingCatalog).kind,
    "reject",
  );
  assert.equal(
    resolveProfileScreenRequest("/p/coding-profile/chat", codingCatalog).kind,
    "reject",
  );
  assert.equal(resolveProfileScreenRequest("/coding", codingCatalog).kind, "reject");
});

test("HostBootstrap accepts the canonical PackAPI success envelope", async (context) => {
  const originalFetch = globalThis.fetch;
  context.after(() => {
    globalThis.fetch = originalFetch;
  });
  globalThis.fetch = async () => new Response(JSON.stringify({
    success: true,
    data: { dynamic_host: catalog },
    error: null,
  }), { status: 200 });

  assert.deepEqual(await fetchDynamicCatalog(), catalog);
});

test("HostBootstrap returns capability data from the canonical PackAPI envelope", async (context) => {
  const originalFetch = globalThis.fetch;
  let requestBody: Record<string, unknown> | null = null;
  context.after(() => {
    globalThis.fetch = originalFetch;
  });
  globalThis.fetch = async (_input, init) => {
    requestBody = JSON.parse(String(init?.body)) as Record<string, unknown>;
    return new Response(JSON.stringify({
      success: true,
      data: { content: [{ type: "text", text: "Ready" }] },
      error: null,
    }), { status: 200 });
  };

  assert.deepEqual(
    await invokeCapability(invocation()),
    { content: [{ type: "text", text: "Ready" }] },
  );
  assert.ok(requestBody);
  const body = requestBody as Record<string, unknown>;
  const { request_id, expires_at, ...stableBody } = body;
  assert.equal(typeof request_id, "string");
  assert.equal(typeof expires_at, "number");
  assert.deepEqual(stableBody, {
    profile_id: "defaults",
    profile_revision: "profile-1",
    activation_id: "activation:defaults-1",
    plan_hash: "plan-1",
    catalog_hash: catalog.catalog_hash,
    contribution_id: "defaults.conversation.complete",
    owner_pack_id: "defaultspack",
    contract_id: "conversation.turn.v1",
    payload: { messages: [{ role: "user", content: "Hello" }] },
  });
});

test("HostBootstrap preserves typed PackAPI failure codes for stale refresh", async (context) => {
  const originalFetch = globalThis.fetch;
  context.after(() => {
    globalThis.fetch = originalFetch;
  });
  globalThis.fetch = async () => new Response(JSON.stringify({
    success: false,
    data: { code: "STALE_CATALOG" },
    error: "The catalog changed",
  }), { status: 409 });

  await assert.rejects(
    invokeCapability(invocation()),
    (error: unknown) => error instanceof FrontendCapabilityError
      && error.code === "STALE_CATALOG"
      && error.message === "The catalog changed",
  );
});
