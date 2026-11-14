/** Build-time pi wrapper. Original source bytes are preserved and pinned. */
import { createHash } from "node:crypto";
import { readFile, writeFile, mkdir, copyFile, mkdtemp, rm } from "node:fs/promises";
import { execFile } from "node:child_process";
import os from "node:os";
import { parseArgs, promisify } from "node:util";
import { fileURLToPath } from "node:url";
import path from "node:path";
import { addInventoryWorld, inventoryFiles, inventoryPorts, inventoryWorldExport, inventoryEntryExport } from "./pi-wrapper-config.js";

const tools = { esbuild: "0.28.2", "componentize-js": "0.23.0", "preview2-shim": "0.28.0", typebox: "0.34.52" };
const digest = (bytes) => createHash("sha256").update(bytes).digest("hex");
const here = path.dirname(fileURLToPath(import.meta.url));

export const readPinnedSource = async (pack) => {
  const provenance = JSON.parse(await readFile(path.join(pack, "upstream.json"), "utf8"));
  if (provenance.origin === "local-unverified") {
    if (provenance.commit !== undefined || provenance.repository !== undefined) throw new Error("local source must not claim an upstream pin");
  } else if (!/^[a-f0-9]{40}$/.test(provenance.commit)) throw new Error("upstream commit must be pinned");
  if (typeof provenance.stored_path !== "string" || !/^upstream\/[^/\\]+$/.test(provenance.stored_path) || [".", ".."].includes(path.posix.basename(provenance.stored_path))) throw new Error("invalid stored source path");
  const original = await readFile(path.join(pack, provenance.stored_path));
  if (digest(original) !== provenance.sha256) throw new Error("upstream source pin mismatch");
  return { provenance, original };
};

/** Compile a caller-selected pinned wrapper; no runtime source discovery. */
export const wrapPi = async (packDirectory, packId = path.basename(packDirectory), { commandInventory = false } = {}) => {
  if (!/^local\.[a-z][a-z0-9-]+$/.test(packId)) throw new Error("unsigned wrapper must use a local.* Pack ID");
  const pack = path.resolve(packDirectory);
  const { provenance, original } = await readPinnedSource(pack);
  const source = path.join(pack, provenance.stored_path);
  const shim = path.join(here, "pi-shim/index.js");
  const aiShim = path.join(here, "pi-shim/pi-ai.js");
  const factoryImport = commandInventory
    ? `import {createExtensionWithCommandInventory as createExtension} from ${JSON.stringify(path.join(here, "pi-shim/command-inventory.js"))};`
    : `import {createExtension} from ${JSON.stringify(shim)};`;
  const entry = `import factory from ${JSON.stringify(source)};
${factoryImport}
import {display} from 'tobkiri:core/host-io@1.0.0';
import {isCancelled} from 'tobkiri:core/flow@1.0.0';
let extension;
const get=()=>extension??=createExtension(factory,{notify:(text,level)=>display('notification',JSON.stringify({text,level})),hasUI:()=>false,isCancelled});
export const lifecycle={init:()=>{get();},shutdown:()=>{extension=undefined;}};
export const piExtension={load:()=>get().load(),invokeTool:(...a)=>get().invokeTool(...a),onEvent:(...a)=>get().onEvent(...a),runCommand:(...a)=>get().runCommand(...a)};${inventoryEntryExport(commandInventory)}`;
  const esbuild = await import("esbuild");
  const result = await esbuild.build({
    stdin: { contents: entry, resolveDir: pack, sourcefile: "wrapper-entry.mjs" },
    bundle: true, format: "esm", platform: "neutral", target: "es2022", write: false,
    outfile: "wrapper.js", sourcemap: "external", sourcesContent: true,
    alias: { "@earendil-works/pi-coding-agent": shim,
             "@earendil-works/pi-ai": aiShim },
    external: ["tobkiri:core/*"],
    // Local mode accepts a single self-authored file, with only the reviewed pi aliases.
    plugins: provenance.origin === "local-unverified" ? [{
      name: "local-source-imports",
      setup(build) {
        build.onResolve({ filter: /.*/, namespace: "file" }, (args) => {
          if (args.path === source) return { path: source, namespace: "tobkiri-local-pi" };
        });
        build.onResolve({ filter: /.*/, namespace: "tobkiri-local-pi" }, ({ path: imported }) => {
          if (imported === "@earendil-works/pi-coding-agent") return { path: shim };
          if (imported === "@earendil-works/pi-ai") return { path: aiShim };
          return { errors: [{ text: `unsupported local source import: ${imported}` }] };
        });
        build.onLoad({ filter: /.*/, namespace: "tobkiri-local-pi" }, () => ({
          contents: original,
          loader: /\.[cm]?tsx?$/.test(source) ? "ts" : "js",
        }));
      },
    }] : [],
  });
  const bundle = result.outputFiles.find((file) => file.path.endsWith("wrapper.js"));
  const sourceMap = result.outputFiles.find((file) => file.path.endsWith(".map"));
  const witDirectory = path.join(pack, "wit");
  await writeFile(path.join(pack, "wrapper.js"), bundle.contents);
  // A private process isolates both preview2-shim's global preopens and the
  // compiler environment without changing the environment of a library caller.
  const scratch = await mkdtemp(path.join(os.tmpdir(), "tobkiri-pi-"));
  let component, imports;
  try {
    const env = Object.fromEntries(["PATH", "TMPDIR", "TEMP", "TMP"]
      .filter((name) => process.env[name] !== undefined).map((name) => [name, process.env[name]]));
    await promisify(execFile)(process.execPath, [path.join(here, "wrap-pi-componentize.mjs"),
      path.join(pack, "wrapper.js"), witDirectory, scratch], { env });
    component = await readFile(path.join(scratch, "component.wasm"));
    imports = JSON.parse(await readFile(path.join(scratch, "imports.json"), "utf8"));
  } finally {
    await rm(scratch, { recursive: true, force: true });
  }
  await mkdir(pack, { recursive: true });
  await writeFile(path.join(pack, "component.wasm"), component);
  await writeFile(path.join(pack, "wrapper.js.map"), sourceMap.contents);
  const receipt = { format: "tobkiri.component-build/1", language: "javascript", tools,
    upstream: provenance, original_sha256: digest(original), bundle_sha256: digest(bundle.contents),
    source_map_sha256: digest(sourceMap.contents), artifact_sha256: digest(component),
    artifact_bytes: component.length, imports, production_enabled: false };
  await writeFile(path.join(pack, "component.build.json"), `${JSON.stringify(receipt, null, 2)}\n`);
  await writeFile(path.join(pack, "config.json"), "{}\n");
  const files = ["component.wasm", "component.build.json", "config.json", "wrapper.js", "wrapper.js.map", "upstream.json",
    provenance.stored_path, "wit/pack.wit", "wit/deps/core.wit", "wit/deps/pi-adapter.wit"];
  files.push(...inventoryFiles(commandInventory));
  const provides = [{ shape: "local.pi-adapter.pi-extension", version: "1.0", optional: false }];
  provides.push(...inventoryPorts(commandInventory));
  const manifest = { format: "tobkiri.pack/1", id: packId, version: "0.1.0", publisher_key: null,
    runtime: { kind: "wasm", component: "component.wasm", toolchain: "esbuild/0.28.2;componentize-js/0.23.0", config: { path: "config.json" } },
    shapes: [], ports: { provides, consumes: [], untyped: [] },
    flows: [], overlays: [], stores: [], ui: null,
    permissions: { network: { origins: [] }, fs: [], proc: [], pty: false, secrets: [],
      storage: { private: false, public_read: false, public_write: false }, native: false, cross_profile_storage: false,
      audit_read: false, audit_append: true, host_io: ["display"], ports: [], mediator: false,
      wasm: { fuel_per_call: 2000000000, memory_mib: 128, wall_ms: 300000, table_elements: 65536, output_bytes: 4194304 },
      loops: { max_iterations: 16, max_wall_ms: 300000, max_cost_units: 100000 } },
    files: await Promise.all(files.sort().map(async (name) => {
      const content = await readFile(path.join(pack, name));
      return { path: name, sha256: `sha256:${digest(content)}`, size: content.length };
    })) };
  await writeFile(path.join(pack, "pack.json"), `${JSON.stringify(manifest, null, 2)}\n`);
  return receipt;
};

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  let packDirectory;
  let packId;
  let commandInventory = false;
  if (process.argv.length === 3 && !process.argv[2].startsWith("--")) {
    packDirectory = process.argv[2];
  } else if (process.argv.length === 4 && process.argv.slice(2).includes("--command-inventory") &&
      process.argv.slice(2).filter((arg) => !arg.startsWith("--")).length === 1) {
    packDirectory = path.resolve(process.argv.slice(2).find((arg) => arg !== "--command-inventory"));
    packId = path.basename(packDirectory);
    if (!/^local\.[a-z][a-z0-9-]+$/.test(packId)) throw new Error("unsigned wrapper must use a local.* Pack ID");
    await readPinnedSource(packDirectory);
    const worldPath = path.join(packDirectory, "wit/pack.wit");
    const world = addInventoryWorld(await readFile(worldPath, "utf8"), packId);
    const repo = path.resolve(here, "../..");
    await copyFile(path.join(repo, "packs/shapes/pi-adapter-v1-1/pi-adapter.wit"), path.join(packDirectory, "wit/deps/pi-adapter-v1-1.wit"));
    await writeFile(worldPath, world);
    commandInventory = true;
  } else {
    const { values } = parseArgs({ options: {
      source: { type: "string" }, upstream: { type: "string" }, "local-source": { type: "boolean", default: false },
      out: { type: "string" }, id: { type: "string" },
      "command-inventory": { type: "boolean", default: false },
    }});
    commandInventory = values["command-inventory"];
    if (!values.source || Boolean(values.upstream) === values["local-source"] || !values.out || !/^local\.[a-z][a-z0-9-]+$/.test(values.id ?? "")) {
      throw new Error("usage: node sdk/js/wrap-pi.mjs --source <ext.ts> (--upstream <GitHub repo>@<40hex>:<path> | --local-source) --out <Pack dir> --id local.<name>");
    }
    const content = await readFile(values.source);
    let match;
    if (!values["local-source"]) {
      match = /^(https:\/\/github\.com\/[^/]+\/[^/]+)@([a-f0-9]{40}):(.+)$/.exec(values.upstream);
      if (!match || match[3].startsWith("/") || match[3].split("/").includes("..")) throw new Error("upstream must have a pinned GitHub commit and normalized path");
      const pinned = await fetch(`${match[1].replace("github.com", "raw.githubusercontent.com")}/${match[2]}/${match[3]}`);
      if (!pinned.ok || digest(new Uint8Array(await pinned.arrayBuffer())) !== digest(content)) throw new Error("extension bytes differ from pinned upstream");
    }
    packDirectory = path.resolve(values.out);
    packId = values.id;
    const originalName = path.basename(values.source);
    await mkdir(path.join(packDirectory, "upstream"), { recursive: true });
    await writeFile(path.join(packDirectory, "upstream", originalName), content);
    await writeFile(path.join(packDirectory, "upstream.json"), `${JSON.stringify(values["local-source"]
      ? { origin: "local-unverified", stored_path: `upstream/${originalName}`, sha256: digest(content) }
      : { repository: match[1], commit: match[2], path: match[3], stored_path: `upstream/${originalName}`, sha256: digest(content) }, null, 2)}\n`);
    await mkdir(path.join(packDirectory, "wit/deps"), { recursive: true });
    const repo = path.resolve(here, "../..");
    await copyFile(path.join(repo, "crates/tobkiri-wit/wit/core/core.wit"), path.join(packDirectory, "wit/deps/core.wit"));
    await copyFile(path.join(repo, "packs/shapes/pi-adapter.wit"), path.join(packDirectory, "wit/deps/pi-adapter.wit"));
    if (commandInventory) await copyFile(path.join(repo, "packs/shapes/pi-adapter-v1-1/pi-adapter.wit"), path.join(packDirectory, "wit/deps/pi-adapter-v1-1.wit"));
    await writeFile(path.join(packDirectory, "wit/pack.wit"), `package local:${values.id.split(".")[1]}@1.0.0;\nworld pack {\n include tobkiri:core/pack-minimal@1.0.0;\n import tobkiri:core/host-io@1.0.0;\n export local:pi-adapter/pi-extension@1.0.0;\n${inventoryWorldExport(commandInventory)}}\n`);
  }
  const receipt = await wrapPi(packDirectory, packId, { commandInventory });
  process.stdout.write(`${JSON.stringify({ artifact_bytes: receipt.artifact_bytes, artifact_sha256: receipt.artifact_sha256 })}\n`);
}
