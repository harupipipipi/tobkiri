import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { addCodingWorkspace, CodingWorkspaceCockpit } from "./CodingWorkspaceCockpit";

const workspace = { workspace_id: "ws-exact", label: "Exact folder", root_path: "/exact", trusted: false };

function render(selectedWorkspaceId?: string | null) {
  return renderToStaticMarkup(createElement(CodingWorkspaceCockpit, {
    workspaces: [workspace], selectedWorkspaceId,
    onWorkspaceSelect: () => undefined,
    onWorkspaceCreate: async () => workspace,
    onWorkspacesRefresh: () => undefined,
  }));
}

test("canonical cockpit offers workspace controls and marks observations unavailable", () => {
  const html = render("ws-exact");
  assert.match(html, /Add workspace/);
  assert.match(html, /Refresh workspaces/);
  assert.match(html, /Workspace identity: ws-exact/);
  assert.match(html, /\/exact/);
  assert.match(html, /does not grant trust or approve actions/);
  assert.match(html, /Git status, file observations, reviews, branch changes/);
  assert.match(html, /No checks have been run here/);
  assert.doesNotMatch(html, /Trust workspace|No diff|No terminal runs|clean/);
});

for (const selected of [null, "ws-missing"]) {
  test(`canonical cockpit never substitutes a different workspace for ${selected}`, () => {
    const html = render(selected);
    assert.match(html, /value="" selected=""/);
    assert.doesNotMatch(html, /Workspace identity: ws-exact|\/exact/);
    assert.match(html, selected ? /selected workspace is unavailable/ : /Select or add a workspace/);
  });
}

test("workspace Add waits for the single native mount result", async () => {
  let calls = 0;
  let finish!: (value: typeof workspace) => void;
  const mount = new Promise<typeof workspace>((resolve) => { finish = resolve; });
  const result = addCodingWorkspace(() => { calls += 1; return mount; });
  let settled = false;
  void result.then(() => { settled = true; });
  await Promise.resolve();
  assert.equal(settled, false);
  assert.equal(calls, 1);
  finish(workspace);
  assert.deepEqual(await result, { workspace, error: null });
  assert.equal(calls, 1);
});

test("workspace Add preserves cancellation and displays failures without replay", async () => {
  assert.deepEqual(await addCodingWorkspace(async () => null), { workspace: null, error: null });
  let calls = 0;
  const result = await addCodingWorkspace(async () => {
    calls += 1;
    throw new Error("HTTP 403: mount approval unavailable");
  });
  assert.deepEqual(result, { workspace: null, error: "HTTP 403: mount approval unavailable" });
  assert.equal(calls, 1);
});

test("App mounts the canonical cockpit and preserves the Add promise for visible errors", () => {
  const app = readFileSync(new URL("../../App.tsx", import.meta.url), "utf8");
  const cockpit = readFileSync(new URL("./CodingWorkspaceCockpit.tsx", import.meta.url), "utf8");
  assert.doesNotMatch(app, /<CodingCockpit[\s>]/);
  assert.equal((app.match(/<CodingWorkspaceCockpit[\s>]/g) ?? []).length, 2);
  assert.equal((app.match(/onWorkspaceCreate=\{handleCodingWorkspacePickCreate\}/g) ?? []).length, 2);
  assert.doesNotMatch(cockpit, /codingResources|ChangeReviewPanel|DiffPanel|CheckpointPanel|ApprovalQueue|TerminalPanel/);
  assert.match(cockpit, /message=\{error\}/);
});
