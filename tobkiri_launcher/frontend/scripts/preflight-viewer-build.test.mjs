import assert from "node:assert/strict";
import test from "node:test";

import {
  assertEnoughDiskSpace,
  formatBytes,
  parseMinFreeMb,
} from "./preflight-viewer-build.mjs";

test("parseMinFreeMb uses the fallback for empty values", () => {
  assert.equal(parseMinFreeMb(undefined, 123), 123);
  assert.equal(parseMinFreeMb("", 456), 456);
});

test("parseMinFreeMb rejects invalid overrides", () => {
  assert.throws(() => parseMinFreeMb("0"), /positive number/);
  assert.throws(() => parseMinFreeMb("nope"), /positive number/);
});

test("formatBytes renders binary units", () => {
  assert.equal(formatBytes(512), "512 B");
  assert.equal(formatBytes(1024), "1.0 KiB");
  assert.equal(formatBytes(5 * 1024 * 1024 * 1024), "5.0 GiB");
});

test("assertEnoughDiskSpace passes when available space is above the threshold", () => {
  const result = assertEnoughDiskSpace({
    checkPath: ".",
    minFreeMb: 5,
    statfs: () => ({ bavail: 6, bsize: 1024 * 1024 }),
  });

  assert.equal(result.availableBytes, 6 * 1024 * 1024);
  assert.equal(result.requiredBytes, 5 * 1024 * 1024);
});

test("assertEnoughDiskSpace reports required and available space", () => {
  assert.throws(
    () => assertEnoughDiskSpace({
      checkPath: ".",
      minFreeMb: 5,
      statfs: () => ({ bavail: 4, bsize: 1024 * 1024 }),
    }),
    (error) => {
      assert.match(error.message, /Tobkiri Launcher build preflight failed/);
      assert.match(error.message, /Required: 5.0 MiB\nAvailable: 4.0 MiB/);
      assert.doesNotMatch(error.message, /Rumi Viewer/);
      return true;
    },
  );
});
