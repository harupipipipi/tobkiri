import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import ts from "typescript";

const source = ts.createSourceFile(
  "App.tsx",
  readFileSync(new URL("../App.tsx", import.meta.url), "utf8"),
  ts.ScriptTarget.Latest,
  true,
  ts.ScriptKind.TSX,
);

// Execute the actual App callback without importing its unrelated renderers.
function appExpression(name: string, bindings: Record<string, unknown>): unknown {
  let initializer: ts.Expression | undefined;
  const visit = (node: ts.Node) => {
    if (ts.isVariableDeclaration(node) && node.name.getText(source) === name) {
      assert.equal(initializer, undefined, `duplicate App binding: ${name}`);
      initializer = node.initializer;
    }
    ts.forEachChild(node, visit);
  };
  visit(source);
  assert.ok(initializer, `missing App binding: ${name}`);
  const javascript = ts.transpileModule(`const value = ${initializer.getText(source)};`, {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.None },
  }).outputText;
  const scope = { useCallback: (callback: unknown) => callback, ...bindings };
  return new Function(...Object.keys(scope), `${javascript}\nreturn value;`)(...Object.values(scope));
}

function healthFixture(health: () => Promise<unknown>) {
  const states: unknown[] = [];
  const diagnostics: unknown[] = [];
  const refresh = appExpression("refreshHealth", {
    api: { health },
    consecutiveHealthFailuresRef: { current: 0 },
    lastHealthyAtRef: { current: null },
    setHealth: () => undefined,
    setBackendConnectionState: (value: unknown) => states.push(value),
    setBackendConnectionNote: () => undefined,
    reportClientDiagnostic: async (value: unknown) => { diagnostics.push(value); },
    activeConversationId: null,
    console: { error: () => undefined },
  }) as (reason: string) => Promise<void>;
  return { refresh, states, diagnostics };
}

test("failed bootstrap health cannot mark the backend step ready", async () => {
  const failure = new Error("HTTP 503: backend unavailable");
  const { refresh, states, diagnostics } = healthFixture(async () => { throw failure; });
  const steps: unknown[] = [];
  const bootstrap = appExpression("backendBootstrap", {
    refreshHealth: refresh,
    updateStartupStep: (...values: unknown[]) => steps.push(values),
  }) as Promise<void>;
  await assert.rejects(bootstrap, (error) => error === failure);
  assert.deepEqual(steps, []);
  assert.deepEqual(states, ["offline"]);
  assert.equal(diagnostics.length, 1);
});

test("healthy bootstrap marks ready, while failed background polling is handled", async () => {
  let healthy = true;
  const { refresh, states } = healthFixture(async () => {
    if (!healthy) throw new Error("connection lost");
    return { status: "ok" };
  });
  const steps: unknown[][] = [];
  await appExpression("backendBootstrap", {
    refreshHealth: refresh,
    updateStartupStep: (...values: unknown[]) => steps.push(values),
  });
  assert.deepEqual(steps.map((step) => step.slice(0, 2)), [["backend", "ready"]]);
  healthy = false;
  await refresh("poll");
  await refresh("focus");
  await refresh("poll");
  assert.deepEqual(states, ["online", "degraded", "degraded", "offline"]);
});

test("failed workspace read preserves the last list and stops dependent context loading", async () => {
  const failure = new Error("workspace permission denied");
  const writes: unknown[] = [];
  const load = appExpression("loadCodingWorkspaces", {
    api: { listCodingWorkspaces: async () => { throw failure; } },
    setCodingWorkspaces: (value: unknown) => writes.push(value),
    setSelectedCodingWorkspaceId: () => assert.fail("selection changed after failure"),
  }) as () => Promise<unknown>;
  let contextReads = 0;
  await assert.rejects(load().then(() => { contextReads += 1; }), (error) => error === failure);
  assert.deepEqual(writes, []);
  assert.equal(contextReads, 0);
});

test("refresh button reports a failed workspace read to the App error notice", async () => {
  const errors: unknown[] = [];
  const report = appExpression("reportCodingLoadError", {
    setError: (value: unknown) => errors.push(value),
  });
  const refresh = appExpression("refreshCodingWorkspaces", {
    loadCodingWorkspaces: async () => { throw new Error("HTTP 403"); },
    reportCodingLoadError: report,
  }) as () => void;
  refresh();
  await new Promise<void>((resolve) => setImmediate(resolve));
  assert.deepEqual(errors, ["HTTP 403"]);
});

const workspace = {
  workspace_id: "workspace-1", label: "Work", root_path: "/workspace",
  trusted: false, metadata: {},
};

function codingFixture(read: (id: string) => Promise<typeof workspace>, effectiveWorkspaceId: string | null = "workspace-1") {
  const writes: unknown[] = [];
  const reads: string[] = [];
  let viewEpoch = 1;
  const codingContextReadRevisionRef = { current: 0 };
  const codingReadModeRef = { current: "coding" };
  const liveTaskContextRef = { current: { profileId: "profile-1" } };
  const savedTurnViewFenceRef = { current: {
    capture: () => viewEpoch,
    matches: (ticket: number) => ticket === viewEpoch,
  } };
  const bindings = {
    api: { getSavedCodingWorkspace: async (id: string) => {
      reads.push(id);
      return read(id);
    } },
    effectiveWorkspaceId, codingContextReadRevisionRef, codingReadModeRef,
    liveTaskContextRef, savedTurnViewFenceRef,
    setCodingContext: (value: unknown) => writes.push(value),
  };
  const load = appExpression("loadCodingContext", bindings) as
    (id?: string | null) => Promise<typeof workspace | null>;
  return { load, writes, reads, bindings, codingReadModeRef, liveTaskContextRef,
    changeView: () => { viewEpoch += 1; } };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<T>((done, fail) => { resolve = done; reject = fail; });
  return { promise, resolve, reject };
}

test("coding bootstrap reads the exact owner workspace without legacy context or git calls", async () => {
  const fixture = codingFixture(async (id) => ({ ...workspace, workspace_id: id }));
  assert.deepEqual(await fixture.load("workspace-2"), { ...workspace, workspace_id: "workspace-2" });
  assert.deepEqual(fixture.reads, ["workspace-2"]);
  assert.deepEqual(fixture.writes, [{ snapshotAvailability: "workspace-only", branch: null,
    rootFolder: "/workspace", workspaceId: "workspace-2", directory: ".", files: [] }]);
});

test("coding bootstrap without a selected workspace clears the old display without an owner read", async () => {
  const fixture = codingFixture(async () => assert.fail("no workspace was selected"), null);
  assert.equal(await fixture.load(), null);
  assert.deepEqual(fixture.writes, [null]);
  assert.deepEqual(fixture.reads, []);
});

test("failed exact workspace read clears current display and preserves the original failure", async () => {
  const failure = new Error("workspace unavailable");
  const fixture = codingFixture(async () => { throw failure; });
  await assert.rejects(fixture.load(), (error) => error === failure);
  assert.deepEqual(fixture.writes, [null]);
  assert.deepEqual(fixture.reads, ["workspace-1"]);
});

for (const fence of ["mode", "profile", "view"] as const) {
  for (const outcome of ["success", "failure"] as const) {
    test(`coding bootstrap discards a stale ${fence} ${outcome}`, async () => {
      const owner = deferred<typeof workspace>();
      const fixture = codingFixture(() => owner.promise);
      const pending = fixture.load();
      if (fence === "mode") fixture.codingReadModeRef.current = "chat";
      if (fence === "profile") fixture.liveTaskContextRef.current.profileId = "profile-2";
      if (fence === "view") fixture.changeView();
      if (outcome === "success") owner.resolve(workspace);
      else owner.reject(new Error("stale owner failure"));
      assert.equal(await pending, null);
      assert.deepEqual(fixture.writes, []);
    });
  }
}

test("a newer workspace read fences both late success and failure from the previous selection", async () => {
  for (const lateOutcome of ["success", "failure"] as const) {
    const first = deferred<typeof workspace>();
    const second = deferred<typeof workspace>();
    const fixture = codingFixture((id) => id === "workspace-1" ? first.promise : second.promise);
    const oldRead = fixture.load("workspace-1");
    const currentRead = fixture.load("workspace-2");
    const current = { ...workspace, workspace_id: "workspace-2", root_path: "/second" };
    second.resolve(current);
    assert.deepEqual(await currentRead, current);
    if (lateOutcome === "success") first.resolve(workspace);
    else first.reject(new Error("old read failed"));
    assert.equal(await oldRead, null);
    assert.equal(fixture.writes.length, 1);
    assert.equal((fixture.writes[0] as { workspaceId: string }).workspaceId, "workspace-2");
  }
});

function workspaceSelectionFixture(fixture: ReturnType<typeof codingFixture>) {
  const selected: string[] = [];
  const modes: string[] = [];
  const errors: string[] = [];
  const select = appExpression("handleCodingWorkspaceSelect", {
    ...fixture.bindings,
    cancelPendingMentionAttachments: () => undefined,
    loadCodingContext: fixture.load,
    handleModeChange: (mode: string) => modes.push(mode),
    setSelectedCodingWorkspaceId: (id: string) => selected.push(id),
    setError: (error: string) => errors.push(error),
  }) as (id: string) => void;
  return { select, selected, modes, errors };
}

test("workspace selection commits local state only after the verified owner read", async () => {
  const owner = deferred<typeof workspace>();
  const fixture = codingFixture(() => owner.promise);
  const selection = workspaceSelectionFixture(fixture);
  selection.select("workspace-1");
  assert.deepEqual(selection.selected, []);
  owner.resolve(workspace);
  await new Promise<void>((resolve) => setImmediate(resolve));
  assert.deepEqual(selection.selected, ["workspace-1"]);
  assert.deepEqual(selection.modes, ["coding"]);
  assert.deepEqual(selection.errors, []);
});

for (const fence of ["mode", "profile", "view", "newer selection"] as const) {
  test(`workspace selection cannot commit after a ${fence} change`, async () => {
    const first = deferred<typeof workspace>();
    const second = deferred<typeof workspace>();
    const fixture = codingFixture((id) => id === "workspace-1" ? first.promise : second.promise);
    const selection = workspaceSelectionFixture(fixture);
    selection.select("workspace-1");
    if (fence === "mode") fixture.codingReadModeRef.current = "chat";
    if (fence === "profile") fixture.liveTaskContextRef.current.profileId = "profile-2";
    if (fence === "view") fixture.changeView();
    if (fence === "newer selection") selection.select("workspace-2");
    first.resolve(workspace);
    await new Promise<void>((resolve) => setImmediate(resolve));
    assert.deepEqual(selection.selected, []);
    assert.deepEqual(selection.modes, []);
    assert.deepEqual(selection.errors, []);
    if (fence === "newer selection") {
      second.resolve({ ...workspace, workspace_id: "workspace-2" });
      await new Promise<void>((resolve) => setImmediate(resolve));
      assert.deepEqual(selection.selected, ["workspace-2"]);
    }
  });
}
