import { mkdir, readdir, readFile, rm, writeFile } from "node:fs/promises";
import { dirname, relative, resolve, sep } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

import { canonicalBuildBytes } from "./canonical-build-output.mjs";

const SCRIPT_DIR = dirname(fileURLToPath(import.meta.url));
const FRONTEND_ROOT = resolve(SCRIPT_DIR, "..");
const DEFAULT_DIST_DIR = resolve(FRONTEND_ROOT, "dist");
const PANEL_BUILD_DIR_ENV = "TOBKIRI_PANEL_BUILD_DIR";
const DEFAULT_PANEL_DIR = resolve(
  FRONTEND_ROOT,
  "../../tobkiri_runtime/core_runtime/core_pack/core_control_panel/web",
);
const BUILD_ONLY_FILES = new Set(["build-metrics.json"]);

export function resolvePanelBuildDir(environ = process.env) {
  const configured = environ[PANEL_BUILD_DIR_ENV]?.trim();
  return configured ? resolve(FRONTEND_ROOT, configured) : DEFAULT_PANEL_DIR;
}

function compareNames(left, right) {
  return left < right ? -1 : left > right ? 1 : 0;
}

async function listFiles(current) {
  const entries = await readdir(current, { withFileTypes: true });
  const files = [];
  for (const entry of entries.sort((left, right) => compareNames(left.name, right.name))) {
    const path = resolve(current, entry.name);
    if (entry.isDirectory()) {
      files.push(...await listFiles(path));
    } else if (entry.isFile()) {
      files.push(path);
    } else {
      throw new Error(`panel build contains an unsupported entry: ${path}`);
    }
  }
  return files;
}

async function copyCanonicalFile(sourcePath, distDir, panelDir) {
  const relativePath = relative(distDir, sourcePath).split(sep).join("/");
  const destination = resolve(panelDir, relativePath);
  await mkdir(dirname(destination), { recursive: true });
  const bytes = await readFile(sourcePath);
  const output = canonicalBuildBytes(relativePath, bytes);
  if (!bytes.equals(output)) await writeFile(sourcePath, output);
  await writeFile(destination, output);
}

export async function copyPanelBuild({
  distDir = DEFAULT_DIST_DIR,
  panelDir,
} = {}) {
  panelDir = panelDir ? resolve(panelDir) : resolvePanelBuildDir();
  await rm(panelDir, { recursive: true, force: true });
  await mkdir(panelDir, { recursive: true });
  const files = (await listFiles(distDir)).filter((sourcePath) => {
    const relativePath = relative(distDir, sourcePath).split(sep).join("/");
    return !BUILD_ONLY_FILES.has(relativePath);
  });
  for (const sourcePath of files) {
    await copyCanonicalFile(sourcePath, distDir, panelDir);
  }
  return { distDir, panelDir };
}

if (import.meta.url === pathToFileURL(process.argv[1]).href) {
  copyPanelBuild().catch((error) => {
    console.error(error);
    process.exitCode = 1;
  });
}
