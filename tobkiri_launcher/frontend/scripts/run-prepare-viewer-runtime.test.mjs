import assert from "node:assert/strict";
import test from "node:test";

import {
  python3Commands,
  runPrepareViewerRuntime,
} from "../../scripts/run_prepare_viewer_runtime.mjs";

test("python3Commands prefers Python 3 on Unix hosts", () => {
  assert.deepEqual(python3Commands("darwin"), [
    { command: "python3", arguments: ["-B"] },
    { command: "python", arguments: ["-B"] },
  ]);
});

test("python3Commands uses the Python launcher first on Windows", () => {
  assert.deepEqual(python3Commands("win32"), [
    { command: "py", arguments: ["-3", "-B"] },
    { command: "python", arguments: ["-B"] },
    { command: "python3", arguments: ["-B"] },
  ]);
});

test("runPrepareViewerRuntime falls back from a missing python3 alias", () => {
  const calls = [];
  const status = runPrepareViewerRuntime(["--mode", "dev"], {
    environment: {},
    platform: "darwin",
    spawn(command, args) {
      calls.push([command, args]);
      return command === "python3"
        ? { error: Object.assign(new Error("missing"), { code: "ENOENT" }) }
        : { status: 0 };
    },
  });

  assert.equal(status, 0);
  assert.deepEqual(calls.map(([command]) => command), ["python3", "python"]);
  assert.equal(calls[1][1][0], "-B");
  assert.equal(calls[1][1].at(-2), "--mode");
  assert.equal(calls[1][1].at(-1), "dev");
});

test("a preparer failure is returned without retrying another interpreter", () => {
  const calls = [];
  assert.equal(runPrepareViewerRuntime(["--mode", "dev"], {
    environment: {},
    spawn(command) { calls.push(command); return {status: 7}; },
  }), 7);
  assert.equal(calls.length, 1);
});

test("release preparation uses the bound Python instead of system PATH", () => {
  const calls = [];
  const status = runPrepareViewerRuntime(["--mode", "release"], {
    environment: { TOBKIRI_PACKAGING_PYTHON: "/sealed/venv/bin/python3", TOBKIRI_PACKAGING_PYTHON_SNAPSHOT: "/sealed" },
    spawn(command, args, options) {
      assert.equal(options.cwd, "/sealed");
      calls.push([command, args]);
      return { status: 0 };
    },
  });
  assert.equal(status, 0);
  assert.equal(calls.length, 1);
  assert.equal(calls[0][0], "/sealed/venv/bin/python3");
  assert.deepEqual(calls[0][1].slice(0, 2), ["-I", "-B"]);
});

test("a missing bound interpreter never falls back to an ambient Python", () => {
  const calls = [];
  assert.throws(() => runPrepareViewerRuntime(["--mode", "release"], {
    environment: { TOBKIRI_PACKAGING_PYTHON: "/sealed/venv/bin/python3", TOBKIRI_PACKAGING_PYTHON_SNAPSHOT: "/sealed" },
    spawn(command) {
      calls.push(command);
      return { error: Object.assign(new Error("missing"), { code: "ENOENT" }) };
    },
  }), /Python 3 is required/);
  assert.deepEqual(calls, ["/sealed/venv/bin/python3"]);
});
