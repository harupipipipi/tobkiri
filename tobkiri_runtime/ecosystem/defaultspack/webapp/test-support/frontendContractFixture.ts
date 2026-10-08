import frontendContractMap from "../../defaultspack/frontend_contract_map.v4.json" with { type: "json" };
import { DEFAULTSPACK_CONTRACT_ENDPOINT } from "../src/lib/api";

type FixtureTarget = {
  contribution_id: string;
  contract_id: string;
  operation_id: string;
};
export type FixtureContractRoute = {
  method: string;
  path: string;
  targets: readonly FixtureTarget[];
};
export type FixtureContractBinding = {
  contributionId: string;
  contractId: string;
  operationId: string;
  method: string;
  path: string;
};

const identities = {
  kanbanList: { contributionId: "defaults.kanban.list", contractId: "tobkiri.resource.kanban.v1", operationId: "rumi_kanban_state_store_pack.kanban-state-resource", method: "GET" },
  kanbanCreate: { contributionId: "defaults.kanban.create", contractId: "tobkiri.action.kanban.v1", operationId: "rumi_kanban_state_store_pack.kanban-state-action", method: "POST" },
  desktopsList: { contributionId: "defaults.managed-desktops.desktops-list", contractId: "tobkiri.resource.managed-desktops.v1", operationId: "rumi_sandbox_runtime_pack.desktops-list", method: "GET" },
  runtimeProviders: { contributionId: "defaults.managed-desktops.runtime-providers-read", contractId: "tobkiri.resource.managed-desktops.v1", operationId: "rumi_sandbox_runtime_pack.runtime-providers-read", method: "GET" },
  sandboxTemplates: { contributionId: "defaults.managed-desktops.sandbox-templates-read", contractId: "tobkiri.resource.managed-desktops.v1", operationId: "rumi_sandbox_runtime_pack.sandbox-templates-read", method: "GET" },
} as const;
type FixtureBindingKey = keyof typeof identities;

/** Resolve a test meaning only through its exact shipped formal declaration. */
export function frontendFixtureBinding(
  key: FixtureBindingKey,
  routes: readonly FixtureContractRoute[] = frontendContractMap.routes,
): FixtureContractBinding {
  const identity = identities[key];
  const matches = routes.filter((route) => route.method === identity.method
    && route.targets.length === 1 && route.targets.some((target) => (
      target.contribution_id === identity.contributionId
      && target.contract_id === identity.contractId
      && target.operation_id === identity.operationId
    )));
  if (matches.length !== 1) throw new Error(`Missing or ambiguous formal fixture binding: ${key}`);
  return { ...identity, path: matches[0].path };
}

/** Identify a declared operation carried by the canonical Host transport. */
export function frontendFixtureRequest(
  input: RequestInfo | URL,
  method: string,
  routes: readonly FixtureContractRoute[] = frontendContractMap.routes,
): FixtureContractBinding | null {
  try {
    const url = new URL(String(input), "http://fixture.invalid");
    if (!url.pathname.startsWith(DEFAULTSPACK_CONTRACT_ENDPOINT)) return null;
    const operation = decodeURIComponent(url.pathname.slice(DEFAULTSPACK_CONTRACT_ENDPOINT.length));
    const separator = operation.indexOf(" ");
    const verb = method.toUpperCase();
    if (separator < 0 || operation.slice(0, separator) !== verb) return null;
    const target = operation.slice(separator + 1);
    if (!target.startsWith("/") || target.startsWith("//")) return null;
    const path = new URL(target, url.origin).pathname;
    const matches = routes.filter((route) => route.method === verb && route.path === path);
    if (matches.length !== 1 || matches[0].targets.length !== 1) return null;
    const binding = matches[0].targets[0];
    return { contributionId: binding.contribution_id, contractId: binding.contract_id,
      operationId: binding.operation_id, method: verb, path };
  } catch { return null; }
}

/** Match every formal identity field, rather than an implementation path. */
export function matchesFrontendFixtureBinding(
  actual: FixtureContractBinding | null,
  expected: FixtureContractBinding,
): boolean {
  return actual !== null && actual.contributionId === expected.contributionId
    && actual.contractId === expected.contractId && actual.operationId === expected.operationId
    && actual.method === expected.method && actual.path === expected.path;
}
