import assert from "node:assert/strict";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import {
  checkGeneratedFrontendContractMap,
  generateFrontendContractMap,
} from "./generate-frontend-contract-map.mjs";

test("the checked-in generated map is deterministic and current", async () => {
  const result = await checkGeneratedFrontendContractMap();
  assert.equal(result.rawDigest, "sha256:d216b97849485033b226f28b9eee0989f6db232034bb87f634b1818f792182d4");
  assert.equal(result.runtimeMap.routes.length, 23);
  const capability = result.runtimeMap.routes.find(
    (route) => route.method === "POST" && route.path === "/api/ui/capability/invoke",
  );
  assert.deepEqual(capability?.targets[0], {
    contribution_id: "defaults.conversation.complete",
    contract_id: "conversation.turn.v1",
    operation_id: "complete",
    provider_id: "defaultspack.conversation",
    function_id: "defaultspack.conversation",
    allowed_payload_keys: ["messages"],
  });
  const profileCatalog = result.runtimeMap.routes.find(
    (route) => route.method === "GET" && route.path === "/api/runtime-surface/profiles",
  );
  assert.deepEqual(profileCatalog?.targets, [{
    contribution_id: "defaults.runtime-surface.profile-catalog",
    contract_id: "tobkiri.host.control-presentation.v4",
    operation_id: "profile.catalog.read",
    provider_id: "tobkiri.host.control-presentation",
    function_id: "tobkiri.host.control-presentation",
    allowed_payload_keys: [],
  }]);
  const operationStatus = result.runtimeMap.routes.find(
    (route) => route.method === "GET" && route.path === "/api/runtime-surface/operation-status",
  );
  assert.deepEqual(operationStatus?.targets, [{
    contribution_id: "defaults.runtime-surface.operation-status",
    contract_id: "tobkiri.host.control-presentation.v4",
    operation_id: "operation.status.read",
    provider_id: "tobkiri.host.control-presentation",
    function_id: "tobkiri.host.control-presentation",
    allowed_payload_keys: ["request_id"],
  }]);
  const resolve = result.runtimeMap.routes.find(
    (route) => route.method === "POST" && route.path === "/api/runtime-surface/profile-change/resolve",
  );
  assert.deepEqual(resolve?.targets[0]?.allowed_payload_keys, [
    "profile_id",
    "expected_profile_revision",
    "expected_plan_digest",
    "desired_pack_ids",
    "profile_definition_digest",
    "profile_catalog_digest",
    "bundle_lock_digest",
  ]);
});

test("a stale or tampered canonical artifact fails closed before generation", async () => {
  const root = await mkdtemp(join(tmpdir(), "tobkiri-contract-map-"));
  const sourcePath = join(root, "frontend_contract_map.v4.json");
  const outputPath = join(root, "generatedFrontendContractMap.ts");
  try {
    const source = JSON.parse(await readFile(
      "../../tobkiri_runtime/ecosystem/defaultspack/defaultspack/frontend_contract_map.v4.json",
      "utf8",
    ));
    source.routes = source.routes.filter((route) => route.path !== "/api/runtime-surface/profile");
    await writeFile(sourcePath, JSON.stringify(source), "utf8");
    await assert.rejects(
      generateFrontendContractMap({mapPath: sourcePath, outputPath}),
      /canonical map digest|missing or non-exact route|generation failed/i,
    );
  } finally {
    await rm(root, {recursive: true, force: true});
  }
});
