import assert from "node:assert/strict";
import test from "node:test";
import frontendContractMap from "../../defaultspack/frontend_contract_map.v4.json" with { type: "json" };
import { resolveProfileScreenRequest } from "../src/host/HostBootstrap";
import { applicationBuiltin } from "../src/host/ApplicationBuiltinView";
import { frontendHostFixtureCatalog, frontendHostFixtureScreenPath } from "./frontendHostFixture";
import { defaultspackContractRoute, defaultspackContractUrl } from "../src/lib/api";
import {
  frontendFixtureBinding, frontendFixtureRequest, matchesFrontendFixtureBinding,
  type FixtureContractRoute,
} from "./frontendContractFixture";

test("fixture matching resolves the shipped formal identity and query-bearing transport", () => {
  for (const key of ["chatReferencesList", "chatReferencesResolve", "savedTurnEvents", "toolCatalog", "kanbanList", "kanbanCreate", "desktopsList", "runtimeProviders", "sandboxTemplates",
    "providerConnections", "providerConfigure", "interactiveApprovalGet", "modelProfilesList", "modelProfilesSave", "modelSearch", "modelAccessRead", "modelAccessCatalog"] as const) {
    const binding = frontendFixtureBinding(key);
    const url = defaultspackContractUrl(defaultspackContractRoute(`${binding.path}?fixture=1`), binding.method);
    assert.deepEqual(frontendFixtureRequest(url, binding.method), binding);
    assert.equal(matchesFrontendFixtureBinding(frontendFixtureRequest(url, binding.method), binding), true);
    assert.equal(frontendFixtureRequest(url, binding.method === "GET" ? "POST" : "GET"), null);
    assert.equal(frontendFixtureRequest(binding.path, binding.method), null);
  }
});

test("missing ambiguous and mismatched declarations cannot authorize fixture dispatch", () => {
  const binding = frontendFixtureBinding("desktopsList");
  const route: FixtureContractRoute = { path: binding.path, method: binding.method,
    targets: [{ contribution_id: binding.contributionId, contract_id: binding.contractId, operation_id: binding.operationId }] };
  assert.throws(() => frontendFixtureBinding("desktopsList", []));
  assert.throws(() => frontendFixtureBinding("desktopsList", [route, route]));
  for (const field of ["contribution_id", "contract_id", "operation_id"] as const) {
    const changed = { ...route, targets: [{ ...route.targets[0], [field]: "foreign" }] };
    assert.throws(() => frontendFixtureBinding("desktopsList", [changed]));
    const url = defaultspackContractUrl(defaultspackContractRoute(binding.path), binding.method);
    assert.equal(matchesFrontendFixtureBinding(frontendFixtureRequest(url, binding.method, [changed]), binding), false);
  }
  const url = defaultspackContractUrl(defaultspackContractRoute(binding.path), binding.method);
  assert.equal(frontendFixtureRequest(url, binding.method, [route, route]), null);
  assert.equal(frontendFixtureRequest(url, binding.method, [{ ...route, targets: [...route.targets, ...route.targets] }]), null);
  assert.equal(frontendFixtureRequest("invalid://%", "GET"), null);
});


test("browser ChatApp fixture binds every shipped Chat route to the same captured Profile", () => {
  const catalog = frontendHostFixtureCatalog(true);
  const entries = frontendContractMap.frontend.entries.filter((entry) => entry.implementation === "defaultspack.chat");
  assert.deepEqual(catalog.contributions.map((item) => item.contribution_id), entries.map((entry) => entry.contribution_id));
  for (const entry of entries) {
    const contribution = catalog.contributions.find((item) => item.contribution_id === entry.contribution_id)!;
    assert.equal(contribution.mode, "application_builtin");
    assert.equal(contribution.resolved_profile_id, catalog.profile_id);
    assert.equal(contribution.resolved_profile_revision, catalog.profile_revision);
    assert.equal(contribution.resolved_activation_id, catalog.activation_id);
    assert.equal(contribution.resolved_plan_hash, catalog.plan_hash);
    assert.notEqual(applicationBuiltin(contribution), null);
    assert.deepEqual(resolveProfileScreenRequest(frontendHostFixtureScreenPath(entry.route), catalog), {
      kind: "route", route: entry.route,
    });
  }
  assert.deepEqual(resolveProfileScreenRequest(`/p/${catalog.profile_id}`, catalog), {
    kind: "redirect", destination: frontendHostFixtureScreenPath(catalog.selected_entry_route),
  });
});

test("browser Profile fixtures retain fail-closed identity and capture checks", () => {
  const catalog = frontendHostFixtureCatalog(true);
  for (const path of ["/", "/chat", "/static/chat", "/p/other/chat", "/p/defaults/missing"]) {
    assert.equal(resolveProfileScreenRequest(path, catalog).kind, "reject");
  }
  const path = frontendHostFixtureScreenPath("/chat");
  assert.equal(resolveProfileScreenRequest(path, { ...catalog, quarantined_pack_ids: ["defaultspack"] }).kind, "reject");
  assert.equal(resolveProfileScreenRequest(path, { ...catalog, plan_hash: "stale-plan" }).kind, "reject");
});

test("declarative conversation fixture stays distinct from the Application Chat fixture", () => {
  const catalog = frontendHostFixtureCatalog();
  assert.equal(catalog.contributions.length, 1);
  assert.equal(catalog.contributions[0].mode, "declarative");
  assert.deepEqual(catalog.contributions[0].view, { type: "conversation_v4" });
  assert.equal(applicationBuiltin(catalog.contributions[0]), null);
  assert.deepEqual(resolveProfileScreenRequest(frontendHostFixtureScreenPath("/chat"), catalog), {
    kind: "route", route: "/chat",
  });
  assert.equal(resolveProfileScreenRequest(frontendHostFixtureScreenPath("/coding"), catalog).kind, "reject");
});


test("provider search fixture requires its exact POST declaration and full formal identity", () => {
  const binding = frontendFixtureBinding("modelSearch");
  assert.equal(binding.method, "POST");
  const url = defaultspackContractUrl(defaultspackContractRoute(binding.path), binding.method);
  const route: FixtureContractRoute = { path: binding.path, method: binding.method,
    targets: [{ contribution_id: binding.contributionId, contract_id: binding.contractId, operation_id: binding.operationId }] };
  assert.equal(frontendFixtureRequest(url, "GET"), null);
  assert.equal(frontendFixtureRequest(defaultspackContractUrl(defaultspackContractRoute(binding.path), "GET"), "GET"), null);
  assert.equal(frontendFixtureRequest(`${url}%2Funexpected`, "POST"), null);
  assert.throws(() => frontendFixtureBinding("modelSearch", []));
  assert.throws(() => frontendFixtureBinding("modelSearch", [route, route]));
  for (const field of ["contribution_id", "contract_id", "operation_id"] as const) {
    const changed = { ...route, targets: [{ ...route.targets[0], [field]: "foreign" }] };
    assert.throws(() => frontendFixtureBinding("modelSearch", [changed]));
    assert.equal(matchesFrontendFixtureBinding(frontendFixtureRequest(url, "POST", [changed]), binding), false);
  }
});


test("tool catalog override requires its exact GET declaration and full formal identity", () => {
  const binding = frontendFixtureBinding("toolCatalog");
  assert.equal(binding.method, "GET");
  const url = defaultspackContractUrl(defaultspackContractRoute(binding.path), binding.method);
  const route: FixtureContractRoute = { path: binding.path, method: binding.method,
    targets: [{ contribution_id: binding.contributionId, contract_id: binding.contractId, operation_id: binding.operationId }] };
  const matches = (input: string, method: string, routes?: FixtureContractRoute[]) => (
    matchesFrontendFixtureBinding(frontendFixtureRequest(input, method, routes), binding)
  );
  assert.equal(matches(url, "GET"), true);
  assert.equal(matches(url, "POST"), false);
  assert.equal(matches(defaultspackContractUrl(defaultspackContractRoute(binding.path), "POST"), "POST"), false);
  assert.equal(matches(`${url}%2Funexpected`, "GET"), false);
  const other = frontendFixtureBinding("modelProfilesList");
  assert.equal(matches(defaultspackContractUrl(defaultspackContractRoute(other.path), other.method), other.method), false);
  assert.throws(() => frontendFixtureBinding("toolCatalog", []));
  assert.throws(() => frontendFixtureBinding("toolCatalog", [route, route]));
  for (const field of ["contribution_id", "contract_id", "operation_id"] as const) {
    const changed = { ...route, targets: [{ ...route.targets[0], [field]: "foreign" }] };
    assert.throws(() => frontendFixtureBinding("toolCatalog", [changed]));
    assert.equal(matches(url, "GET", [changed]), false);
  }
});
