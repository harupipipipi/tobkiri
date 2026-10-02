import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { verifyShellBundleManifest } from "./shell-bundle-manifest.mjs";

const here = path.dirname(fileURLToPath(import.meta.url));
const uiDir = path.resolve(here, "../../ui");
const shellPath = path.join(uiDir, "shell.html");
const expectedAssets = new Set([
  "shell-app.css",
  "shell-app.js",
  "shell-defaultspack-app.js",
  "shell-rolldown-runtime.js",
  "shell-vendor.js",
]);

if (fs.existsSync(shellPath)) {
  const shell = fs.readFileSync(shellPath, "utf8");
  for (const asset of ["shell-app.css", "shell-app.js"]) {
    if (!shell.includes(`/static/${asset}`)) {
      throw new Error(`shell.html does not reference /static/${asset}`);
    }
  }
} else {
  console.warn("shell.html is not included in this standalone frontend archive; host-shell reference validation was skipped.");
}

const shellApp = fs.readFileSync(path.join(uiDir, "shell-app.js"), "utf8");
const rootRelativeChunkReference = /(?:from|import\()\s*["']\.\/shell-[^"']+\.js["']/;
if (rootRelativeChunkReference.test(shellApp)) {
  throw new Error("shell-app.js must reference split chunks through /static/, not root-relative siblings");
}

const actualShellAssets = fs
  .readdirSync(uiDir)
  .filter((name) => /^shell-.*\.(?:js|css)$/.test(name))
  .sort();
const expected = [...expectedAssets].sort();
if (JSON.stringify(actualShellAssets) !== JSON.stringify(expected)) {
  throw new Error(
    `shell bundle assets are out of sync. expected=${expected.join(",")} actual=${actualShellAssets.join(",")}`,
  );
}

verifyShellBundleManifest({ webappRoot: path.resolve(here, ".."), uiDir });
