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
  assert.equal(result.rawDigest, "sha256:cc2fb99cc81a372319ed26837f20299264391a7ca4471edc637c3d5e8e5258fd");
  assert.equal(result.runtimeMap.routes.length, 45);
  const stop = result.runtimeMap.routes.find(
    (route) => route.method === "POST" && route.path === "/api/chat/turn/stop",
  );
  assert.deepEqual(stop?.targets, [{
    contribution_id: "defaults.conversations.turn.stop",
    contract_id: "tobkiri.action.turn.stop.v1",
    operation_id: "rumi_turn_runtime_pack.turn-stop",
    provider_id: "rumi_turn_runtime_pack.turn-runtime.stop",
    function_id: "rumi_turn_runtime_pack.turn-runtime.stop",
    allowed_payload_keys: ["turn_id"],
  }]);
  const reconcile = result.runtimeMap.routes.find(
    (route) => route.method === "POST" && route.path === "/api/chat/turn/reconcile",
  );
  assert.deepEqual(reconcile?.targets, [{
    contribution_id: "defaults.conversations.turn.reconcile",
    contract_id: "tobkiri.action.turn.reconcile.v1",
    operation_id: "rumi_turn_runtime_pack.turn-reconcile",
    provider_id: "rumi_turn_runtime_pack.turn-runtime.reconcile",
    function_id: "rumi_turn_runtime_pack.turn-runtime.reconcile",
    allowed_payload_keys: ["turn_id"],
  }]);
  const saved = result.runtimeMap.routes.find(
    (route) => route.method === "POST" && route.path === "/api/chat/turn",
  );
  assert.deepEqual(saved?.targets, [{
    contribution_id: "defaults.conversations.send",
    contract_id: "tobkiri.action.turn.saved.v1",
    operation_id: "rumi_turn_runtime_pack.turn-saved",
    provider_id: "rumi_turn_runtime_pack.turn-runtime.saved",
    function_id: "rumi_turn_runtime_pack.turn-runtime.saved",
    allowed_payload_keys: ["request"],
  }]);
  for (const method of ["PUT", "DELETE"]) {
    const route = result.runtimeMap.routes.find((item) => item.method === method && item.path === "/api/chat/conversation");
    assert.equal(route?.path, "/api/chat/conversation");
    assert.equal(route?.targets[0].contract_id, "tobkiri.action.conversation.manage.v1");
    assert.ok(route?.targets[0].allowed_payload_keys.includes("expected_conversation_revision"));
  }
  for (const path of [
    "/api/ai/profiles", "/api/chat/conversations", "/api/ui/settings",
    "/api/ui/full-catalog", "/api/command-protocol/v1/catalog",
  ]) {
    const read = result.runtimeMap.routes.find((route) => route.path === path);
    assert.equal(read?.method, "GET");
    assert.equal(read?.targets.length, 1);
    assert.deepEqual(read?.targets[0].allowed_payload_keys, path === "/api/ui/settings" ? ["full"] : []);
  }
  const preferences = result.runtimeMap.routes.find(
    (route) => route.method === "PUT" && route.path === "/api/ui/settings",
  );
  assert.deepEqual(preferences?.targets, [{
    contribution_id: "defaults.ui.preferences.write",
    contract_id: "tobkiri.action.ui.preferences.v1",
    operation_id: "tobkiri_ui_settings_pack.preferences-write",
    provider_id: "tobkiri.ui.preferences.write",
    function_id: "tobkiri.ui.preferences.write",
    allowed_payload_keys: ["changes", "expected_revision"],
  }]);
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
