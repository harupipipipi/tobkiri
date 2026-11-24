import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdir, mkdtemp, readFile, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { copyPanelBuild } from "./copy-panel-build.mjs";

test("copyPanelBuild replaces panel output with dist contents", async () => {
  const root = await mkdtemp(join(tmpdir(), "rumi-panel-copy-"));
  const distDir = join(root, "dist");
  const panelDir = join(root, "panel");

  await writeFile(join(root, "placeholder"), "root", "utf8");
  await mkdir(distDir, { recursive: true });
  await writeFile(join(distDir, "index.html"), "<main>ok</main>\r\n", "utf8");
  await writeFile(join(distDir, "app.js"), "console.log('ok');\r\n", "utf8");
  await writeFile(join(distDir, "build-metrics.json"), '{"gzip_bytes":123}\n', "utf8");
  await writeFile(join(distDir, "manifest.json"), "{\r\n  \"z\": 2,\r\n  \"a\": 1,\r\n  \"file\": \"assets\\\\app.js\"\r\n}\r\n", "utf8");
  await mkdir(join(distDir, "nested"), { recursive: true });
  const binary = Buffer.from([0, 255, 1, 254]);
  await writeFile(join(distDir, "nested", "icon.bin"), binary);
  await mkdir(panelDir, { recursive: true });
  await writeFile(join(panelDir, "old.txt"), "old", "utf8");

  await copyPanelBuild({ distDir, panelDir });

  assert.equal(await readFile(join(panelDir, "index.html"), "utf8"), "<main>ok</main>\n");
  assert.equal(await readFile(join(panelDir, "app.js"), "utf8"), "console.log('ok');\n");
  assert.equal(await readFile(join(panelDir, "manifest.json"), "utf8"), '{\n  "a": 1,\n  "file": "assets/app.js",\n  "z": 2\n}\n');
  assert.equal(await readFile(join(distDir, "manifest.json"), "utf8"), '{\n  "a": 1,\n  "file": "assets/app.js",\n  "z": 2\n}\n');
  assert.deepEqual(await readFile(join(panelDir, "nested", "icon.bin")), binary);
  await assert.rejects(readFile(join(panelDir, "build-metrics.json"), "utf8"));
  await assert.rejects(readFile(join(panelDir, "old.txt"), "utf8"));
});

test("copyPanelBuild keeps the release checkout clean after measurement", async () => {
  const root = await mkdtemp(join(tmpdir(), "tobkiri-panel-release-clean-"));
  const distDir = join(root, "dist");
  const panelDir = join(root, "panel");
  await mkdir(distDir, { recursive: true });
  await mkdir(panelDir, { recursive: true });
  await writeFile(join(root, ".gitignore"), "dist/\n", "utf8");
  await writeFile(join(distDir, "index.html"), "<main>ok</main>\n", "utf8");
  await writeFile(
    join(distDir, "build-metrics.json"),
    '{"gzip_bytes":999}\n',
    "utf8",
  );
  await writeFile(join(panelDir, "index.html"), "<main>ok</main>\n", "utf8");
  execFileSync("git", ["init", "--quiet"], { cwd: root });
  execFileSync("git", ["add", "."], { cwd: root });
  execFileSync(
    "git",
    [
      "-c",
      "user.name=Tobkiri Test",
      "-c",
      "user.email=test@invalid",
      "commit",
      "--quiet",
      "-m",
      "fixture",
    ],
    { cwd: root },
  );

  await copyPanelBuild({ distDir, panelDir });

  const status = execFileSync(
    "git",
    ["status", "--porcelain=v1", "--untracked-files=all"],
    { cwd: root, encoding: "utf8" },
  );
  assert.equal(status, "");
  await assert.rejects(readFile(join(panelDir, "build-metrics.json"), "utf8"));
});

test("copyPanelBuild isolates regenerated tracked panel output when configured", async () => {
  const root = await mkdtemp(join(tmpdir(), "tobkiri-panel-isolated-"));
  const distDir = join(root, "dist");
  const trackedPanelDir = join(root, "runtime", "core_control_panel", "web");
  const isolatedPanelDir = join(root, "runner-temp", "tobkiri-panel-build");

  await mkdir(distDir, { recursive: true });
  await mkdir(trackedPanelDir, { recursive: true });
  await writeFile(join(distDir, "index.html"), "<main>regenerated</main>\n", "utf8");
  await writeFile(join(trackedPanelDir, "index.html"), "<main>checked-in</main>\n", "utf8");
  await writeFile(join(root, ".gitignore"), "runner-temp/\n", "utf8");
  execFileSync("git", ["init", "--quiet"], { cwd: root });
  execFileSync("git", ["add", "."], { cwd: root });
  execFileSync(
    "git",
    [
      "-c",
      "user.name=Tobkiri Test",
      "-c",
      "user.email=test@invalid",
      "commit",
      "--quiet",
      "-m",
      "fixture",
    ],
    { cwd: root },
  );

  const previousPanelDir = process.env.TOBKIRI_PANEL_BUILD_DIR;
  process.env.TOBKIRI_PANEL_BUILD_DIR = isolatedPanelDir;
  try {
    await copyPanelBuild({ distDir });

    assert.equal(
      await readFile(join(trackedPanelDir, "index.html"), "utf8"),
      "<main>checked-in</main>\n",
    );
    assert.equal(
      await readFile(join(isolatedPanelDir, "index.html"), "utf8"),
      "<main>regenerated</main>\n",
    );
    assert.equal(
      execFileSync(
        "git",
        ["status", "--porcelain=v1", "--untracked-files=all"],
        { cwd: root, encoding: "utf8" },
      ),
      "",
    );
  } finally {
    if (previousPanelDir === undefined) {
      delete process.env.TOBKIRI_PANEL_BUILD_DIR;
    } else {
      process.env.TOBKIRI_PANEL_BUILD_DIR = previousPanelDir;
    }
  }
});
