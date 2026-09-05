import { createHash } from "node:crypto";
import { readFile, writeFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const SCRIPT_DIR = dirname(fileURLToPath(import.meta.url));
const FRONTEND_ROOT = resolve(SCRIPT_DIR, "..");
const MAP_PATH = resolve(
  FRONTEND_ROOT,
  "../../tobkiri_runtime/ecosystem/defaultspack/defaultspack/frontend_contract_map.v4.json",
);
const OUTPUT_PATH = resolve(FRONTEND_ROOT, "src/lib/generatedFrontendContractMap.ts");
const MAP_ARTIFACT_PATH = "defaultspack/frontend_contract_map.v4.json";
const PINNED_ARTIFACT_DIGEST =
  "sha256:b6fba6eafe1809167a9dc7f5c88948557a46f08e3f059a46d3250fc79930841f";

const RUNTIME_TARGET_SPECS = [
  {
    method: "GET",
    path: "/api/runtime-surface/profile",
    contribution_id: "defaults.runtime-surface.profile",
    contract_id: "tobkiri.host.control-presentation.v4",
    operation_id: "profile.read",
    allowed_payload_keys: ["expected_profile_revision", "expected_plan_digest"],
  },
  {
    method: "GET",
    path: "/api/runtime-surface/profiles",
    contribution_id: "defaults.runtime-surface.profile-catalog",
    contract_id: "tobkiri.host.control-presentation.v4",
    operation_id: "profile.catalog.read",
    allowed_payload_keys: [],
  },
  {
    method: "GET",
    path: "/api/runtime-surface/operation-status",
    contribution_id: "defaults.runtime-surface.operation-status",
    contract_id: "tobkiri.host.control-presentation.v4",
    operation_id: "operation.status.read",
    allowed_payload_keys: ["request_id"],
  },
  {
    method: "GET",
    path: "/api/runtime-surface/settings",
    contribution_id: "defaults.runtime-surface.settings",
    contract_id: "tobkiri.host.control-presentation.v4",
    operation_id: "settings.read",
    allowed_payload_keys: [],
  },
  {
    method: "GET",
    path: "/api/runtime-surface/topology/packs",
    contribution_id: "defaults.runtime-surface.packs",
    contract_id: "tobkiri.host.control-presentation.v4",
    operation_id: "topology.packs.read",
    allowed_payload_keys: ["expected_profile_revision", "expected_plan_digest"],
  },
  {
    method: "GET",
    path: "/api/runtime-surface/topology/contracts",
    contribution_id: "defaults.runtime-surface.contracts",
    contract_id: "tobkiri.host.control-presentation.v4",
    operation_id: "topology.contracts.read",
    allowed_payload_keys: ["expected_profile_revision", "expected_plan_digest"],
  },
  {
    method: "GET",
    path: "/api/runtime-surface/topology/operations",
    contribution_id: "defaults.runtime-surface.operations",
    contract_id: "tobkiri.host.control-presentation.v4",
    operation_id: "topology.operations.read",
    allowed_payload_keys: ["expected_profile_revision", "expected_plan_digest"],
  },
  {
    method: "GET",
    path: "/api/runtime-surface/topology/principals",
    contribution_id: "defaults.runtime-surface.principals",
    contract_id: "tobkiri.host.control-presentation.v4",
    operation_id: "topology.principals.read",
    allowed_payload_keys: ["expected_profile_revision", "expected_plan_digest"],
  },
  {
    method: "POST",
    path: "/api/runtime-surface/profile-change/resolve",
    contribution_id: "defaults.runtime-surface.profile-change.resolve",
    contract_id: "tobkiri.host.control-presentation.v4",
    operation_id: "profile.change.resolve",
    allowed_payload_keys: [
      "profile_id",
      "expected_profile_revision",
      "expected_plan_digest",
      "desired_pack_ids",
      "profile_definition_digest",
      "profile_catalog_digest",
      "bundle_lock_digest",
    ],
  },
  {
    method: "POST",
    path: "/api/runtime-surface/profile-change/review",
    contribution_id: "defaults.runtime-surface.profile-change.review",
    contract_id: "tobkiri.host.control-presentation.v4",
    operation_id: "profile.change.review",
    allowed_payload_keys: ["candidate_id", "candidate_digest"],
  },
  {
    method: "POST",
    path: "/api/runtime-surface/profile-change/approve",
    contribution_id: "defaults.runtime-surface.profile-change.approve",
    contract_id: "tobkiri.host.control-presentation.v4",
    operation_id: "profile.change.approve",
    allowed_payload_keys: ["candidate_id", "candidate_digest"],
  },
  {
    method: "POST",
    path: "/api/runtime-surface/profile-change/activate",
    contribution_id: "defaults.runtime-surface.profile-change.activate",
    contract_id: "tobkiri.host.control-presentation.v4",
    operation_id: "profile.change.activate",
    allowed_payload_keys: ["approval_id", "approval_digest"],
  },
];

function digestBytes(bytes) {
  return `sha256:${createHash("sha256").update(bytes).digest("hex")}`;
}

function isRecord(value) {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function fail(message) {
  throw new Error(`Frontend Contract Map generation failed: ${message}`);
}

function exactKeys(value, keys) {
  return isRecord(value)
    && Object.keys(value).length === keys.length
    && keys.every((key) => Object.prototype.hasOwnProperty.call(value, key));
}

function validateSourceMap(map, rawDigest) {
  if (!exactKeys(map, ["schema", "pack_id", "routes"])) {
    fail("canonical map envelope is not exact");
  }
  if (map.schema !== "io.tobkiri.frontend-contract-map.v4" || map.pack_id !== "defaultspack") {
    fail("canonical map identity is invalid");
  }
  if (rawDigest !== PINNED_ARTIFACT_DIGEST) {
    fail(`canonical map digest is ${rawDigest}, expected ${PINNED_ARTIFACT_DIGEST}`);
  }
  if (!Array.isArray(map.routes)) fail("canonical map routes are not an array");

  const targetKeys = [
    "contribution_id",
    "contract_id",
    "operation_id",
    "provider_id",
    "function_id",
    "allowed_payload_keys",
  ];
  const normalizedRoutes = map.routes.map((route) => {
    if (!exactKeys(route, ["method", "path", "presentation", "targets"])) {
      fail("canonical map route fields are not exact");
    }
    if (
      (route.method !== "GET" && route.method !== "POST")
      || typeof route.path !== "string"
      || typeof route.presentation !== "string"
      || !Array.isArray(route.targets)
      || route.targets.length === 0
    ) {
      fail("canonical map route is invalid");
    }
    for (const target of route.targets) {
      if (!exactKeys(target, targetKeys)
        || !Array.isArray(target.allowed_payload_keys)
        || target.allowed_payload_keys.some((item) => typeof item !== "string")) {
        fail(`canonical map target fields are not exact for ${route.method} ${route.path}`);
      }
    }
    return route;
  });
  const routeKeys = new Set();
  for (const route of normalizedRoutes) {
    const key = `${route.method} ${route.path}`;
    if (routeKeys.has(key)) fail(`canonical map contains a duplicate route ${key}`);
    routeKeys.add(key);
  }

  for (const spec of RUNTIME_TARGET_SPECS) {
    const route = normalizedRoutes.find(
      (candidate) => candidate.method === spec.method && candidate.path === spec.path,
    );
    if (!route || route.presentation !== "broker_result" || route.targets.length !== 1) {
      fail(`missing or non-exact route ${spec.method} ${spec.path}`);
    }
    const target = route.targets[0];
    if (
      target.contribution_id !== spec.contribution_id
      || target.contract_id !== spec.contract_id
      || target.operation_id !== spec.operation_id
      || target.provider_id !== "tobkiri.host.control-presentation"
      || target.function_id !== "tobkiri.host.control-presentation"
      || JSON.stringify(target.allowed_payload_keys) !== JSON.stringify(spec.allowed_payload_keys)
    ) {
      fail(`route ${spec.method} ${spec.path} target metadata does not match the frozen contract`);
    }
  }

  return {
    schema: map.schema,
    pack_id: map.pack_id,
    artifact_path: MAP_ARTIFACT_PATH,
    artifact_digest: rawDigest,
    routes: normalizedRoutes,
  };
}

function renderGeneratedModule(runtimeMap) {
  const expectedRoutes = Object.fromEntries(
    runtimeMap.routes.map((route) => {
      return [
        `${route.method} ${route.path}`,
        {
          presentation: route.presentation,
          targets: route.targets.map((target) => ({
            contribution_id: target.contribution_id,
            contract_id: target.contract_id,
            operation_id: target.operation_id,
            provider_id: target.provider_id,
            function_id: target.function_id,
            allowed_payload_keys: target.allowed_payload_keys,
          })),
        },
      ];
    }),
  );

  return `/* eslint-disable */
// GENERATED FILE. Do not edit by hand.
// Source: ${MAP_ARTIFACT_PATH}
// Raw source digest: ${runtimeMap.artifact_digest}
import type {FrontendContractMethod} from './api';

export interface GeneratedFrontendContractTarget {
  contribution_id: string;
  contract_id: string;
  operation_id: string;
  provider_id: string;
  function_id: string;
  allowed_payload_keys: string[];
}

export interface GeneratedFrontendContractRoute {
  method: FrontendContractMethod;
  path: string;
  presentation: string;
  targets: GeneratedFrontendContractTarget[];
}

export interface GeneratedFrontendContractMap {
  schema: 'io.tobkiri.frontend-contract-map.v4';
  pack_id: 'defaultspack';
  artifact_path: 'defaultspack/frontend_contract_map.v4.json';
  artifact_digest: string;
  routes: GeneratedFrontendContractRoute[];
}

export const PINNED_FRONTEND_CONTRACT_MAP_ARTIFACT_DIGEST = ${JSON.stringify(runtimeMap.artifact_digest)} as const;

export const GENERATED_FRONTEND_CONTRACT_MAP: GeneratedFrontendContractMap = ${JSON.stringify(runtimeMap, null, 2)};

const EXPECTED_ROUTES = ${JSON.stringify(expectedRoutes, null, 2)} as const;

function isDigest(value: unknown): value is string {
  return typeof value === 'string' && /^sha256:[0-9a-f]{64}$/.test(value);
}

function exactStringArray(value: unknown): value is string[] {
  return Array.isArray(value) && value.every((item) => typeof item === 'string');
}

function exactKeys(value: unknown, keys: string[]): value is Record<string, unknown> {
  return typeof value === 'object'
    && value !== null
    && !Array.isArray(value)
    && Object.keys(value).length === keys.length
    && keys.every((key) => Object.prototype.hasOwnProperty.call(value, key));
}

/** Validate the generated binding against its pinned canonical artifact. */
export function validateGeneratedFrontendContractMap(
  value: unknown,
  expectedArtifactDigest = PINNED_FRONTEND_CONTRACT_MAP_ARTIFACT_DIGEST,
): GeneratedFrontendContractMap {
  if (!exactKeys(value, ['schema', 'pack_id', 'artifact_path', 'artifact_digest', 'routes'])) {
    throw new Error('Generated frontend Contract Map artifact is stale or tampered.');
  }
  const map = value as Partial<GeneratedFrontendContractMap>;
  if (
    map.schema !== 'io.tobkiri.frontend-contract-map.v4'
    || map.pack_id !== 'defaultspack'
    || map.artifact_path !== 'defaultspack/frontend_contract_map.v4.json'
    || !isDigest(map.artifact_digest)
    || map.artifact_digest !== expectedArtifactDigest
    || !Array.isArray(map.routes)
    || map.routes.length !== Object.keys(EXPECTED_ROUTES).length
  ) {
    throw new Error('Generated frontend Contract Map artifact is stale or tampered.');
  }
  const seen = new Set<string>();
  for (const route of map.routes) {
    if (!exactKeys(route, ['method', 'path', 'presentation', 'targets'])) {
      throw new Error('Generated frontend Contract Map route is invalid.');
    }
    if (
      (route.method !== 'GET' && route.method !== 'POST')
      || typeof route.path !== 'string'
      || typeof route.presentation !== 'string'
      || !Array.isArray(route.targets)
      || route.targets.length === 0
    ) {
      throw new Error('Generated frontend Contract Map route is invalid.');
    }
    const key = \`\${route.method} \${route.path}\`;
    const expected = EXPECTED_ROUTES[key];
    if (
      !expected
      || seen.has(key)
      || route.presentation !== expected.presentation
      || route.targets.length !== expected.targets.length
    ) {
      throw new Error('Generated frontend Contract Map target set is invalid.');
    }
    route.targets.forEach((target, index) => {
      const expectedTarget = expected.targets[index];
      if (!exactKeys(target, [
        'contribution_id',
        'contract_id',
        'operation_id',
        'provider_id',
        'function_id',
        'allowed_payload_keys',
      ]) || (
        target.contribution_id !== expectedTarget.contribution_id
        || target.contract_id !== expectedTarget.contract_id
        || target.operation_id !== expectedTarget.operation_id
        || target.provider_id !== expectedTarget.provider_id
        || target.function_id !== expectedTarget.function_id
        || !exactStringArray(target.allowed_payload_keys)
        || target.allowed_payload_keys.length !== expectedTarget.allowed_payload_keys.length
        || target.allowed_payload_keys.some((item, itemIndex) => item !== expectedTarget.allowed_payload_keys[itemIndex])
      )) {
        throw new Error('Generated frontend Contract Map target metadata is invalid.');
      }
    });
    seen.add(key);
  }
  return map as GeneratedFrontendContractMap;
}

export interface VerifiedGeneratedTarget {
  method: FrontendContractMethod;
  logical_target: string;
  contract_id: string;
  operation_id: string;
  contribution_id: string;
  provider_id: string;
  function_id: string;
  allowed_payload_keys: string[];
  map_artifact_digest: string;
  source_ref: string;
}

export function generatedRouteFor(
  map: GeneratedFrontendContractMap,
  method: FrontendContractMethod,
  logicalTarget: string,
): GeneratedFrontendContractRoute {
  const verified = validateGeneratedFrontendContractMap(map);
  const route = verified.routes.find((candidate) => candidate.method === method && candidate.path === logicalTarget);
  if (!route) {
    throw new Error(\`Generated frontend Contract Map has no exact route for \${method} \${logicalTarget}.\`);
  }
  return route;
}

export function generatedTargetFor(
  map: GeneratedFrontendContractMap,
  method: FrontendContractMethod,
  logicalTarget: string,
): VerifiedGeneratedTarget {
  const route = generatedRouteFor(map, method, logicalTarget);
  if (!route || route.targets.length !== 1) {
    throw new Error(\`Generated frontend Contract Map has no exact target for \${method} \${logicalTarget}.\`);
  }
  const target = route.targets[0];
  return {
    method: route.method,
    logical_target: route.path,
    contract_id: target.contract_id,
    operation_id: target.operation_id,
    contribution_id: target.contribution_id,
    provider_id: target.provider_id,
    function_id: target.function_id,
    allowed_payload_keys: [...target.allowed_payload_keys],
    map_artifact_digest: map.artifact_digest,
    source_ref: \`pack-artifact://\${map.pack_id}/\${map.artifact_path}\`,
  };
}

export const VERIFIED_GENERATED_FRONTEND_CONTRACT_MAP = validateGeneratedFrontendContractMap(
  GENERATED_FRONTEND_CONTRACT_MAP,
);
export const VERIFIED_GENERATED_RUNTIME_TARGETS = VERIFIED_GENERATED_FRONTEND_CONTRACT_MAP;
`;
}

export async function generateFrontendContractMap({
  mapPath = MAP_PATH,
  outputPath = OUTPUT_PATH,
} = {}) {
  const rawBytes = await readFile(mapPath);
  const rawDigest = digestBytes(rawBytes);
  const sourceMap = JSON.parse(rawBytes.toString("utf8"));
  const runtimeMap = validateSourceMap(sourceMap, rawDigest);
  const generated = renderGeneratedModule(runtimeMap);
  await writeFile(outputPath, generated, "utf8");
  return { mapPath, outputPath, rawDigest, runtimeMap };
}

export async function checkGeneratedFrontendContractMap({
  mapPath = MAP_PATH,
  outputPath = OUTPUT_PATH,
} = {}) {
  const rawBytes = await readFile(mapPath);
  const rawDigest = digestBytes(rawBytes);
  const sourceMap = JSON.parse(rawBytes.toString("utf8"));
  const runtimeMap = validateSourceMap(sourceMap, rawDigest);
  const expected = renderGeneratedModule(runtimeMap);
  const actual = await readFile(outputPath, "utf8");
  if (actual !== expected) {
    throw new Error(`Generated frontend Contract Map is out of date: ${outputPath}`);
  }
  return { mapPath, outputPath, rawDigest, runtimeMap };
}

const isMain = process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href;
if (isMain) {
  const check = process.argv.includes("--check");
  const task = check ? checkGeneratedFrontendContractMap : generateFrontendContractMap;
  task().then(({ rawDigest }) => {
    console.log(`${check ? "checked" : "generated"} frontend Contract Map (${rawDigest})`);
  }).catch((error) => {
    console.error(error instanceof Error ? error.message : error);
    process.exitCode = 1;
  });
}
