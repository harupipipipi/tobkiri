import path from "node:path";
import { fileURLToPath } from "node:url";
import { writeShellBundleManifest } from "./shell-bundle-manifest.mjs";

const here = path.dirname(fileURLToPath(import.meta.url));
const webappRoot = path.resolve(here, "..");
const uiDir = path.resolve(webappRoot, "../ui");

writeShellBundleManifest({ webappRoot, uiDir });
