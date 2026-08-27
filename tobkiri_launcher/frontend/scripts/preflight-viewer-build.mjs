import { existsSync, statfsSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

export const DEFAULT_MIN_FREE_MB = 5120;

const SCRIPT_DIR = dirname(fileURLToPath(import.meta.url));
const FRONTEND_ROOT = resolve(SCRIPT_DIR, "..");
const DEFAULT_CHECK_PATH = resolve(FRONTEND_ROOT, "../src-tauri");

function toNumber(value) {
  return typeof value === "bigint" ? Number(value) : value;
}

export function parseMinFreeMb(value, fallback = DEFAULT_MIN_FREE_MB) {
  if (value == null || value === "") {
    return fallback;
  }

  const parsed = Number(value);
  if (!Number.isFinite(parsed) || parsed <= 0) {
    throw new Error(`RUMI_VIEWER_MIN_FREE_MB must be a positive number, got ${value}`);
  }
  return parsed;
}

export function formatBytes(bytes) {
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  let value = bytes;
  let unitIndex = 0;
  while (value >= 1024 && unitIndex < units.length - 1) {
    value /= 1024;
    unitIndex += 1;
  }
  return `${value.toFixed(unitIndex === 0 ? 0 : 1)} ${units[unitIndex]}`;
}

export function nearestExistingPath(path) {
  let current = resolve(path);
  while (!existsSync(current)) {
    const parent = dirname(current);
    if (parent === current) {
      return parent;
    }
    current = parent;
  }
  return current;
}

export function getAvailableBytes(path, statfs = statfsSync) {
  const stats = statfs(nearestExistingPath(path));
  return toNumber(stats.bavail) * toNumber(stats.bsize);
}

export function assertEnoughDiskSpace({
  checkPath = DEFAULT_CHECK_PATH,
  minFreeMb = parseMinFreeMb(process.env.RUMI_VIEWER_MIN_FREE_MB),
  statfs = statfsSync,
} = {}) {
  const availableBytes = getAvailableBytes(checkPath, statfs);
  const requiredBytes = minFreeMb * 1024 * 1024;

  if (availableBytes < requiredBytes) {
    throw new Error(
      [
        "Tobkiri Launcher build preflight failed: not enough free disk space.",
        `Checked path: ${nearestExistingPath(checkPath)}`,
        `Required: ${formatBytes(requiredBytes)}`,
        `Available: ${formatBytes(availableBytes)}`,
        "Free disk space before running `cargo tauri dev`, or set RUMI_VIEWER_MIN_FREE_MB=<MB> to override for a known-good environment.",
      ].join("\n"),
    );
  }

  return { availableBytes, requiredBytes };
}

if (import.meta.url === pathToFileURL(process.argv[1]).href) {
  try {
    const { availableBytes, requiredBytes } = assertEnoughDiskSpace();
    console.log(
      `Tobkiri Launcher build preflight passed: ${formatBytes(availableBytes)} free (${formatBytes(requiredBytes)} required).`,
    );
  } catch (error) {
    console.error(error.message);
    process.exitCode = 1;
  }
}
