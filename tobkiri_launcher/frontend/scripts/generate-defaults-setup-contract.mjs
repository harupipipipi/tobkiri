import {createHash} from "node:crypto";
import {readFile, writeFile} from "node:fs/promises";
import {dirname, resolve} from "node:path";
import {fileURLToPath, pathToFileURL} from "node:url";

const SCRIPT_DIR = dirname(fileURLToPath(import.meta.url));
const FRONTEND_ROOT = resolve(SCRIPT_DIR, "..");
const SCHEMA_PATH = resolve(
  FRONTEND_ROOT,
  "../../tobkiri_runtime/tobkiri_protocol/schemas/defaults_setup_v4.schema.json",
);
const OUTPUT_PATH = resolve(FRONTEND_ROOT, "src/lib/generatedDefaultsSetupContract.ts");
const SOURCE_REF = "tobkiri_protocol/schemas/defaults_setup_v4.schema.json";

function fail(message) {
  throw new Error(`Defaults setup Contract generation failed: ${message}`);
}

function digestBytes(bytes) {
  return `sha256:${createHash("sha256").update(bytes).digest("hex")}`;
}

function objectDefinition(value, label) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    fail(`${label} is not an object definition`);
  }
  if (value.type !== "object" || value.additionalProperties !== false) {
    fail(`${label} must be an exact object`);
  }
  if (!value.properties || typeof value.properties !== "object" || Array.isArray(value.properties)) {
    fail(`${label} has no properties`);
  }
  if (!Array.isArray(value.required)) fail(`${label} has no required field list`);
  const properties = Object.keys(value.properties).sort();
  const required = [...value.required].sort();
  if (JSON.stringify(properties) !== JSON.stringify(required)) {
    fail(`${label} has optional or unrequired serialized fields`);
  }
  return [...value.required];
}

function objectDefinitionWithOptional(value, label, optionalKeys) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    fail(`${label} is not an object definition`);
  }
  if (value.type !== "object" || value.additionalProperties !== false) {
    fail(`${label} must be an exact object`);
  }
  if (!value.properties || typeof value.properties !== "object" || Array.isArray(value.properties)) {
    fail(`${label} has no properties`);
  }
  if (!Array.isArray(value.required)) fail(`${label} has no required field list`);
  const properties = Object.keys(value.properties).sort();
  const expected = [...value.required, ...optionalKeys].sort();
  if (JSON.stringify(properties) !== JSON.stringify(expected)) {
    fail(`${label} has optional or unrequired serialized fields`);
  }
  return {required: [...value.required], optional: [...optionalKeys]};
}

function stringEnum(value, label) {
  if (!value || !Array.isArray(value.enum) || value.enum.length === 0
    || value.enum.some((item) => typeof item !== "string")) {
    fail(`${label} is not a finite string enum`);
  }
  return [...value.enum];
}

function readContract(schema) {
  if (schema.$id !== "https://schemas.tobkiri.local/io.tobkiri/defaults-setup.v4.schema.json") {
    fail("schema identity is invalid");
  }
  const definitions = schema.$defs;
  if (!definitions || typeof definitions !== "object" || Array.isArray(definitions)) {
    fail("schema definitions are unavailable");
  }
  const binding = objectDefinitionWithOptional(
    definitions.binding,
    "binding",
    ["authority_mode"],
  );
  return {
    setupKeys: objectDefinition(schema, "setup response"),
    profileKeys: objectDefinition(definitions.recommendedProfile, "recommended Profile"),
    profileShellKeys: objectDefinition(definitions.profileShell, "Profile Shell"),
    packKeys: objectDefinition(definitions.packProjection, "Pack projection"),
    confirmationKeys: objectDefinition(definitions.confirmation, "confirmation"),
    baseKeys: objectDefinition(definitions.base, "confirmed Base"),
    confirmedShellKeys: objectDefinition(definitions.confirmedShell, "confirmed Shell"),
    bindingKeys: binding.required,
    bindingOptionalKeys: binding.optional,
    principalKeys: objectDefinition(definitions.functionPrincipal, "function principal"),
    setupStates: stringEnum(schema.properties.state, "setup state"),
    domainKinds: stringEnum(definitions.binding.properties.domain_kind, "domain kind"),
    executionKinds: stringEnum(
      definitions.binding.properties.execution_kind,
      "execution kind",
    ),
    requiredTransaction: [...schema.properties.required_transaction.const],
  };
}

function constant(name, value) {
  return `export const ${name} = ${JSON.stringify(value)} as const;`;
}

function render(contract, sourceDigest) {
  return `/* eslint-disable */
// GENERATED FILE. Do not edit by hand.
// Source: ${SOURCE_REF}
// Raw source digest: ${sourceDigest}

${constant("DEFAULTS_SETUP_KEYS", contract.setupKeys)}
${constant("DEFAULTS_PROFILE_KEYS", contract.profileKeys)}
${constant("DEFAULTS_PROFILE_SHELL_KEYS", contract.profileShellKeys)}
${constant("DEFAULTS_PACK_KEYS", contract.packKeys)}
${constant("DEFAULTS_CONFIRMATION_KEYS", contract.confirmationKeys)}
${constant("DEFAULTS_BASE_KEYS", contract.baseKeys)}
${constant("DEFAULTS_CONFIRMED_SHELL_KEYS", contract.confirmedShellKeys)}
${constant("DEFAULTS_BINDING_KEYS", contract.bindingKeys)}
${constant("DEFAULTS_BINDING_OPTIONAL_KEYS", contract.bindingOptionalKeys)}
${constant("DEFAULTS_FUNCTION_PRINCIPAL_KEYS", contract.principalKeys)}
${constant("DEFAULTS_SETUP_STATES", contract.setupStates)}
${constant("DEFAULTS_BINDING_DOMAIN_KINDS", contract.domainKinds)}
${constant("DEFAULTS_BINDING_EXECUTION_KINDS", contract.executionKinds)}
${constant("DEFAULTS_REQUIRED_TRANSACTION", contract.requiredTransaction)}
`;
}

async function expectedOutput(schemaPath) {
  const bytes = await readFile(schemaPath);
  const schema = JSON.parse(bytes.toString("utf8"));
  const sourceDigest = digestBytes(bytes);
  return {
    sourceDigest,
    contract: readContract(schema),
    output: render(readContract(schema), sourceDigest),
  };
}

export async function generateDefaultsSetupContract({
  schemaPath = SCHEMA_PATH,
  outputPath = OUTPUT_PATH,
} = {}) {
  const expected = await expectedOutput(schemaPath);
  await writeFile(outputPath, expected.output, "utf8");
  return {...expected, schemaPath, outputPath};
}

export async function checkGeneratedDefaultsSetupContract({
  schemaPath = SCHEMA_PATH,
  outputPath = OUTPUT_PATH,
} = {}) {
  const expected = await expectedOutput(schemaPath);
  const actual = await readFile(outputPath, "utf8");
  if (actual !== expected.output) {
    fail(`generated frontend module is out of date: ${outputPath}`);
  }
  return {...expected, schemaPath, outputPath};
}

const isMain = process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href;
if (isMain) {
  const check = process.argv.includes("--check");
  const task = check ? checkGeneratedDefaultsSetupContract : generateDefaultsSetupContract;
  task().then(({sourceDigest}) => {
    console.log(`${check ? "checked" : "generated"} Defaults setup Contract (${sourceDigest})`);
  }).catch((error) => {
    console.error(error instanceof Error ? error.message : error);
    process.exitCode = 1;
  });
}
