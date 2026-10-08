import assert from "node:assert/strict";
import test from "node:test";
import { defaultspackContractRoute, defaultspackContractUrl } from "../src/lib/api";
import {
  frontendFixtureBinding, frontendFixtureRequest, matchesFrontendFixtureBinding,
  type FixtureContractRoute,
} from "./frontendContractFixture";

test("fixture matching resolves the shipped formal identity and query-bearing transport", () => {
  for (const key of ["kanbanList", "kanbanCreate", "desktopsList", "runtimeProviders", "sandboxTemplates"] as const) {
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
