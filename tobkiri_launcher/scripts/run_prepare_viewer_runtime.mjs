#!/usr/bin/env node
/**
 * Run the runtime preparer with a Python 3 interpreter available on this host.
 *
 * Tauri evaluates build hooks outside an activated virtual environment on some
 * platforms. In particular, macOS installations commonly provide `python3`
 * without a `python` alias. Keep the interpreter choice at this development
 * boundary instead of relying on an ambient shell alias.
 */

import { spawnSync } from "node:child_process";
import { isAbsolute } from "node:path";
import { fileURLToPath } from "node:url";

const PREPARER_PATH = fileURLToPath(
  new URL("./prepare_viewer_runtime.py", import.meta.url),
);
/**
 * Return Python 3 commands in host-preferred order.
 *
 * @param {string} platform Node platform name.
 * @returns {{ command: string, arguments: string[] }[]} Python invocations.
 */
export const python3Commands = (platform = process.platform) => {
  if (platform === "win32") {
    return [
      { command: "py", arguments: ["-3", "-B"] },
      { command: "python", arguments: ["-B"] },
      { command: "python3", arguments: ["-B"] },
    ];
  }

  return [
    { command: "python3", arguments: ["-B"] },
    { command: "python", arguments: ["-B"] },
  ];
};

/**
 * Run prepare_viewer_runtime.py and return its exit status.
 *
 * @param {string[]} args Arguments forwarded to the preparer.
 * @param {{
 *   environment?: Record<string, string | undefined>,
 *   platform?: string,
 *   spawn?: typeof spawnSync,
 * }} options Process controls used by tests.
 * @returns {number} Process exit status.
 */
export const runPrepareViewerRuntime = (
  args,
  {
    environment = process.env,
    platform = process.platform,
    spawn = spawnSync,
  } = {},
) => {
  // Release packaging binds a Python with the pinned validation dependencies.
  // The Tauri runner deliberately puts Apple's system tools first in PATH;
  // its older python3 is not the packaging interpreter.
  const packagingPython = environment.TOBKIRI_PACKAGING_PYTHON;
  const packagingRoot = environment.TOBKIRI_PACKAGING_PYTHON_SNAPSHOT;
  if (packagingPython && (!isAbsolute(packagingPython) || !packagingRoot || !isAbsolute(packagingRoot))) {
    throw new Error("Packaging Python requires an absolute interpreter and snapshot root");
  }
  const commands = packagingPython
    ? [{ command: packagingPython, arguments: ["-I", "-B"] }]
    : python3Commands(platform);

  for (const candidate of commands) {
    const result = spawn(
      candidate.command,
      [...candidate.arguments, PREPARER_PATH, ...args],
      { env: environment, stdio: "inherit", cwd: packagingPython ? packagingRoot : undefined },
    );
    if (result.error?.code === "ENOENT") {
      continue;
    }
    if (result.error) {
      throw result.error;
    }
    if (result.status === null) {
      return 1;
    }
    return result.status;
  }

  const names = commands.map(({ command }) => command).join(", ");
  throw new Error(`Python 3 is required to prepare the Launcher runtime (${names})`);
};

const main = () => {
  try {
    return runPrepareViewerRuntime(process.argv.slice(2));
  } catch (error) {
    console.error(`Tobkiri Launcher runtime preparation failed: ${error.message}`);
    return 1;
  }
};

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  process.exitCode = main();
}
