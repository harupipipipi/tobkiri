import test from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdtemp, mkdir, writeFile, rm } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { readPinnedSource } from "../wrap-pi.mjs";

const sha256 = (value) => createHash("sha256").update(value).digest("hex");
test("local provenance remains explicit and source bytes are checked", async () => {
  const dir = await mkdtemp(path.join(os.tmpdir(), "pi-source-test-"));
  try {
    await mkdir(path.join(dir, "upstream"));
    const source = "export default () => {};\n";
    await writeFile(path.join(dir, "upstream/source.js"), source);
    const metadata = { origin: "local-unverified", stored_path: "upstream/source.js", sha256: sha256(source) };
    const save = () => writeFile(path.join(dir, "upstream.json"), JSON.stringify(metadata));
    await save();
    assert.equal((await readPinnedSource(dir)).provenance.origin, "local-unverified");
    await writeFile(path.join(dir, "upstream/source.js"), "changed");
    await assert.rejects(readPinnedSource(dir), /pin mismatch/);
    metadata.commit = "a".repeat(40);
    await save();
    await assert.rejects(readPinnedSource(dir), /must not claim/);
    delete metadata.origin;
    metadata.commit = "unverified";
    await save();
    await assert.rejects(readPinnedSource(dir), /must be pinned/);
    metadata.commit = "a".repeat(40);
    metadata.stored_path = "../outside";
    await save();
    await assert.rejects(readPinnedSource(dir), /invalid stored source path/);
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
});

const compilerAvailable = await Promise.all([
  import("esbuild").then(() => true, () => false),
  import("@bytecodealliance/componentize-js").then(() => true, () => false),
]).then((available) => available.every(Boolean));
if (process.env.TOBKIRI_REQUIRE_PI_COMPILER === "1") {
  assert.ok(compilerAvailable, "locked Pi compiler dependencies are missing");
}

test("offline CLI and library builds preserve local provenance and caller environment", { skip: !compilerAvailable, timeout: 600000 }, async () => {
  const { execFile } = await import("node:child_process");
  const { promisify } = await import("node:util");
  const { readFile } = await import("node:fs/promises");
  const { wrapPi } = await import("../wrap-pi.mjs");
  const dir = await mkdtemp(path.join(os.tmpdir(), "pi-local-build-test-"));
  const previous = process.env.TOBKIRI_WRAP_TEST_SENTINEL;
  try {
    const source = path.join(dir, "extension.js");
    const pack = path.join(dir, "local.example");
    await writeFile(source, `import { defineTool } from "@earendil-works/pi-coding-agent";
export default function (pi) {
  pi.registerTool(defineTool({ name: "test-local", parameters: {}, execute: () => ({ content: [] }) }));
}
`);
    const cli = new URL("../wrap-pi.mjs", import.meta.url);
    await promisify(execFile)(process.execPath, [fileURLToPath(cli), "--source", source, "--local-source", "--out", pack, "--id", "local.example"], { timeout: 300000 });
    const provenance = JSON.parse(await readFile(path.join(pack, "upstream.json"), "utf8"));
    const manifest = JSON.parse(await readFile(path.join(pack, "pack.json"), "utf8"));
    assert.equal(provenance.origin, "local-unverified");
    assert.equal(manifest.id, "local.example");
    assert.equal(manifest.publisher_key, null);
    assert.ok(manifest.files.some((file) => file.path === "upstream.json"));
    process.env.TOBKIRI_WRAP_TEST_SENTINEL = "preserved";
    const receipt = await wrapPi(pack);
    assert.equal(process.env.TOBKIRI_WRAP_TEST_SENTINEL, "preserved");
    assert.equal(receipt.upstream.origin, "local-unverified");
    const forbidden = 'import "some-unreviewed-package"; export default () => {};';
    await writeFile(path.join(pack, provenance.stored_path), forbidden);
    provenance.sha256 = sha256(forbidden);
    await writeFile(path.join(pack, "upstream.json"), JSON.stringify(provenance));
    await assert.rejects(wrapPi(pack), /unsupported local source import/);
  } finally {
    if (previous === undefined) delete process.env.TOBKIRI_WRAP_TEST_SENTINEL;
    else process.env.TOBKIRI_WRAP_TEST_SENTINEL = previous;
    await rm(dir, { recursive: true, force: true });
  }
});

test("existing pinned wrappers still verify", async () => {
  for (const name of ["hello", "protected-paths"]) {
    const dir = fileURLToPath(new URL(`../../../packs/local.pi-example-${name}/`, import.meta.url));
    const { provenance } = await readPinnedSource(dir);
    assert.match(provenance.commit, /^[a-f0-9]{40}$/);
    assert.equal(provenance.origin, undefined);
  }
});
