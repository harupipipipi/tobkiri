import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";

export const MANIFEST_NAME = ".shell-bundle-manifest.json";

const SOURCE_DIRECTORIES = ["src", "public", "scripts"];
const SOURCE_FILES = [
  "index.html",
  "package.json",
  "package-lock.json",
  "tsconfig.json",
  "vite.config.ts",
];
const GENERATED_SHELL_ASSET = /^shell-.*\.(?:js|css)$/;

function sha256(payload) {
  return crypto.createHash("sha256").update(payload).digest("hex");
}

function regularFiles(root) {
  if (!fs.existsSync(root)) return [];
  const entries = [];
  for (const entry of fs.readdirSync(root, { withFileTypes: true })) {
    const entryPath = path.join(root, entry.name);
    if (entry.isSymbolicLink()) {
      throw new Error(`shell bundle manifest refuses symlink: ${entryPath}`);
    }
    if (entry.isDirectory()) {
      entries.push(...regularFiles(entryPath));
    } else if (entry.isFile()) {
      entries.push(entryPath);
    }
  }
  return entries;
}

function relativeFiles(root, relativePaths) {
  const files = new Set();
  for (const relativePath of relativePaths) {
    const absolutePath = path.join(root, relativePath);
    if (fs.existsSync(absolutePath)) files.add(absolutePath);
  }
  return [...files].sort();
}

function snapshot(root, files) {
  const entries = files
    .map((filePath) => {
      const payload = fs.readFileSync(filePath);
      return {
        path: path.relative(root, filePath).split(path.sep).join("/"),
        size: payload.length,
        sha256: sha256(payload),
      };
    })
    .sort((left, right) =>
      left.path < right.path ? -1 : left.path > right.path ? 1 : 0,
    );
  const canonical = entries
    .map((entry) => `${entry.path}\0${entry.size}\0${entry.sha256}`)
    .join("\n");
  return { digest: sha256(canonical), entries };
}

function sourceSnapshot(webappRoot) {
  const recursiveFiles = SOURCE_DIRECTORIES.flatMap((directory) =>
    regularFiles(path.join(webappRoot, directory)),
  );
  return snapshot(webappRoot, [
    ...new Set([...recursiveFiles, ...relativeFiles(webappRoot, SOURCE_FILES)]),
  ]);
}

function bundleSnapshot(uiDir) {
  const generated = fs
    .readdirSync(uiDir)
    .filter((name) => GENERATED_SHELL_ASSET.test(name))
    .map((name) => path.join(uiDir, name));
  const shellHtml = path.join(uiDir, "shell.html");
  if (!fs.existsSync(shellHtml)) {
    throw new Error(`shell bundle entry is missing: ${shellHtml}`);
  }
  return snapshot(uiDir, [...generated, shellHtml]);
}

export function buildShellBundleManifest({ webappRoot, uiDir }) {
  return {
    schema: "io.tobkiri.defaultspack-shell-bundle-manifest.v1",
    source: sourceSnapshot(webappRoot),
    bundle: bundleSnapshot(uiDir),
  };
}

export function writeShellBundleManifest({ webappRoot, uiDir }) {
  const manifest = buildShellBundleManifest({ webappRoot, uiDir });
  const manifestPath = path.join(uiDir, MANIFEST_NAME);
  fs.writeFileSync(manifestPath, `${JSON.stringify(manifest, null, 2)}\n`, "utf8");
  return manifestPath;
}

export function verifyShellBundleManifest({ webappRoot, uiDir }) {
  const manifestPath = path.join(uiDir, MANIFEST_NAME);
  if (!fs.existsSync(manifestPath)) {
    throw new Error(
      `shell bundle source manifest is missing; run npm run build before packaging: ${manifestPath}`,
    );
  }
  let actual;
  try {
    actual = JSON.parse(fs.readFileSync(manifestPath, "utf8"));
  } catch (error) {
    throw new Error(`shell bundle source manifest is malformed: ${error}`);
  }
  const expected = buildShellBundleManifest({ webappRoot, uiDir });
  if (JSON.stringify(actual) !== JSON.stringify(expected)) {
    throw new Error(
      "defaultspack source/bundle drift detected; run npm run build and commit the generated ui bundle",
    );
  }
  return actual;
}
