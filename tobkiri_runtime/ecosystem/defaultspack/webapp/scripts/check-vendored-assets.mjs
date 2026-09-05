import { createHash } from "node:crypto";
import { existsSync, readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const WEBAPP_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const PACK_ROOT = path.resolve(WEBAPP_ROOT, "..");
const PUBLIC_ROOT = path.join(WEBAPP_ROOT, "public");
const UI_ROOT = path.join(PACK_ROOT, "ui");
const REQUIRE_UI_ASSETS = process.argv.includes("--require-ui");
const GENERATED_UI_ROOTS = [
  {
    root: UI_ROOT,
    label: "../ui",
  },
];

const MEDIAPIPE_TASKS_VISION = {
  package: "@mediapipe/tasks-vision",
  version: "0.10.35",
  license: "Apache-2.0",
  integrity: "sha512-HOvadwVRE6JC+45nyYhmnywnr5h/J8KZvOeUNVOG9q/0875pZgItznFB9bRTvLc264YSJqiZ1NsIpCStJw/egg==",
};

const ASSETS = [
  {
    path: "mediapipe/wasm/vision_wasm_internal.js",
    sha256: "11fdcbe35b15e222bd60f02c1be7e5f8dd8a73721a0a55cf8adcf38b88977b9e",
    source: "@mediapipe/tasks-vision@0.10.35 wasm bundle",
  },
  {
    path: "mediapipe/wasm/vision_wasm_internal.wasm",
    sha256: "6a5c64584c2ab61c763b6e204afbdbc7ce1caf7f5216187322bca8df94f646bc",
    source: "@mediapipe/tasks-vision@0.10.35 wasm bundle",
  },
  {
    path: "mediapipe/wasm/vision_wasm_module_internal.js",
    sha256: "e23be0c990685926cc0a13a46936015527f36e95adf965250ea08d3b9fd28ef2",
    source: "@mediapipe/tasks-vision@0.10.35 wasm bundle",
  },
  {
    path: "mediapipe/wasm/vision_wasm_module_internal.wasm",
    sha256: "617b8e0248dbd27e9d7ece4218004eae4cefb499196d1bb4fa0e3fef21708756",
    source: "@mediapipe/tasks-vision@0.10.35 wasm bundle",
  },
  {
    path: "mediapipe/wasm/vision_wasm_nosimd_internal.js",
    sha256: "df375e4da93bbc1078481da6e2e519fd55ea125a14a00379a9b7bb395fb56c80",
    source: "@mediapipe/tasks-vision@0.10.35 wasm bundle",
  },
  {
    path: "mediapipe/wasm/vision_wasm_nosimd_internal.wasm",
    sha256: "8a3092d34c79d3f57e6ba8592105e8a90f6b07c27891ffecd14cca428bfd3e31",
    source: "@mediapipe/tasks-vision@0.10.35 wasm bundle",
  },
  {
    path: "models/hand_landmarker.task",
    sha256: "fbc2a30080c3c557093b5ddfc334698132eb341044ccee322ccf8bcf3607cde1",
    source: "https://storage.googleapis.com/mediapipe-models/hand_landmarker/hand_landmarker/float16/1/hand_landmarker.task",
  },
];

const NOTICE_PATH = "mediapipe/NOTICE.md";

function sha256(filePath) {
  const bytes = readFileSync(filePath);
  const hashBytes = filePath.endsWith(".js")
    ? Buffer.from(bytes.toString("utf8").replace(/\r\n/g, "\n"), "utf8")
    : bytes;
  return createHash("sha256").update(hashBytes).digest("hex");
}

function normalizedText(filePath) {
  return readFileSync(filePath, "utf8").replace(/\r\n?/g, "\n");
}

function assertPackageLock(errors) {
  const lockPath = path.join(WEBAPP_ROOT, "package-lock.json");
  const lock = JSON.parse(readFileSync(lockPath, "utf8"));
  const entry = lock.packages?.[`node_modules/${MEDIAPIPE_TASKS_VISION.package}`];
  if (!entry) {
    errors.push(`missing package-lock entry for ${MEDIAPIPE_TASKS_VISION.package}`);
    return;
  }
  for (const field of ["version", "license", "integrity"]) {
    if (entry[field] !== MEDIAPIPE_TASKS_VISION[field]) {
      errors.push(
        `${MEDIAPIPE_TASKS_VISION.package} ${field} changed: expected ${MEDIAPIPE_TASKS_VISION[field]}, got ${entry[field]}`,
      );
    }
  }
}

function checkAsset(asset, errors) {
  const publicPath = path.join(PUBLIC_ROOT, asset.path);
  if (!existsSync(publicPath)) {
    errors.push(`missing webapp public asset: public/${asset.path}`);
    return;
  }

  const publicHash = sha256(publicPath);
  if (publicHash !== asset.sha256) {
    errors.push(`public/${asset.path} hash changed: expected ${asset.sha256}, got ${publicHash}`);
  }

  for (const generatedRoot of GENERATED_UI_ROOTS) {
    const generatedPath = path.join(generatedRoot.root, asset.path);
    if (!existsSync(generatedPath)) {
      if (REQUIRE_UI_ASSETS) errors.push(`missing packaged ${generatedRoot.label} asset: ${generatedRoot.label}/${asset.path}`);
      continue;
    }
    const generatedHash = sha256(generatedPath);
    if (generatedHash !== asset.sha256) {
      errors.push(`${generatedRoot.label}/${asset.path} hash changed: expected ${asset.sha256}, got ${generatedHash}`);
    }
    if (publicHash !== generatedHash) {
      errors.push(`public/${asset.path} and ${generatedRoot.label}/${asset.path} are out of sync`);
    }
  }
}

function assertNotice(errors) {
  const noticePath = path.join(PUBLIC_ROOT, NOTICE_PATH);
  if (!existsSync(noticePath)) {
    errors.push(`missing MediaPipe notice: public/${NOTICE_PATH}`);
    return;
  }
  const notice = normalizedText(noticePath);
  for (const required of [
    MEDIAPIPE_TASKS_VISION.package,
    MEDIAPIPE_TASKS_VISION.version,
    MEDIAPIPE_TASKS_VISION.license,
    MEDIAPIPE_TASKS_VISION.integrity,
    "hand_landmarker.task",
    "SHA-256",
    ...ASSETS.flatMap((asset) => [asset.path, asset.sha256]),
  ]) {
    if (!notice.includes(required)) errors.push(`public/${NOTICE_PATH} is missing ${required}`);
  }
  for (const generatedRoot of GENERATED_UI_ROOTS) {
    const generatedNotice = path.join(generatedRoot.root, NOTICE_PATH);
    if (!existsSync(generatedNotice)) {
      if (REQUIRE_UI_ASSETS) errors.push(`missing packaged MediaPipe notice: ${generatedRoot.label}/${NOTICE_PATH}`);
      continue;
    }
    if (normalizedText(generatedNotice) !== notice) {
      errors.push(`public/${NOTICE_PATH} and ${generatedRoot.label}/${NOTICE_PATH} are out of sync`);
    }
  }
}

const errors = [];
assertPackageLock(errors);
assertNotice(errors);
for (const asset of ASSETS) checkAsset(asset, errors);

if (errors.length > 0) {
  console.error("Vendored MediaPipe asset check failed:");
  for (const error of errors) console.error(`- ${error}`);
  process.exit(1);
}

console.log(`Vendored MediaPipe public assets match ${MEDIAPIPE_TASKS_VISION.package}@${MEDIAPIPE_TASKS_VISION.version}`);
console.log(
  REQUIRE_UI_ASSETS
    ? "Packaged ../ui copies are required and verified."
    : "Packaged ../ui copies are generated by npm run build and verified only when present.",
);
for (const asset of ASSETS) {
  console.log(`- ${asset.path}: ${asset.sha256} (${asset.source})`);
}
