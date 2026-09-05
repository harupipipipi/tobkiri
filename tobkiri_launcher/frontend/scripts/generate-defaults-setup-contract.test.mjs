import assert from "node:assert/strict";
import {mkdtemp, readFile, rm, writeFile} from "node:fs/promises";
import {tmpdir} from "node:os";
import {join} from "node:path";
import test from "node:test";

import {
  checkGeneratedDefaultsSetupContract,
  generateDefaultsSetupContract,
} from "./generate-defaults-setup-contract.mjs";

test("checked-in Defaults setup bindings are generated from the canonical schema", async () => {
  const result = await checkGeneratedDefaultsSetupContract();
  assert.deepEqual(result.contract.bindingKeys, [
    "caller_function_id",
    "pack_id",
    "artifact_digest",
    "function_principal",
    "contract_id",
    "operation_id",
    "domain_kind",
    "executable_catalog_digest",
    "variant_id",
    "platform",
    "architecture",
    "runtime_abi",
    "backend",
    "execution_kind",
    "authority_reference",
    "requested_scope_digest",
    "adapter_digests",
  ]);
  assert.deepEqual(result.contract.bindingOptionalKeys, ["authority_mode"]);
});

test("schema optional-field drift fails closed before frontend generation", async () => {
  const root = await mkdtemp(join(tmpdir(), "tobkiri-defaults-contract-"));
  const schemaPath = join(root, "defaults_setup_v4.schema.json");
  const outputPath = join(root, "generated.ts");
  try {
    const schema = JSON.parse(await readFile(
      "../../tobkiri_runtime/tobkiri_protocol/schemas/defaults_setup_v4.schema.json",
      "utf8",
    ));
    schema.$defs.binding.properties.unbound_field = {type: "string"};
    await writeFile(schemaPath, JSON.stringify(schema), "utf8");
    await assert.rejects(
      generateDefaultsSetupContract({schemaPath, outputPath}),
      /optional or unrequired serialized fields/,
    );
  } finally {
    await rm(root, {recursive: true, force: true});
  }
});
