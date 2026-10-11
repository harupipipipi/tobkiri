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

for (const failedPart of ["context", "branch"] as const) {
  test(`failed ${failedPart} read remains visible without fabricating context`, async () => {
    const failure = new Error(`${failedPart} unavailable`);
    const writes: unknown[] = [];
    const errors: unknown[] = [];
    const load = appExpression("loadCodingContext", {
      api: {
        getCodingContext: async () => {
          if (failedPart === "context") throw failure;
          return { branch: "main", root_folder: "/workspace", files: [] };
        },
        getGitBranch: async () => {
          if (failedPart === "branch") throw failure;
          return { branches: ["main"] };
        },
      },
      codingDirectory: ".",
      effectiveWorkspaceId: "workspace-1",
      reportCodingLoadError: (value: unknown) => errors.push(value),
      setCodingContext: (value: unknown) => writes.push(value),
    }) as () => Promise<void>;
    if (failedPart === "context") {
      await assert.rejects(load(), (error) => error === failure);
      assert.deepEqual(writes, [null]);
    } else {
      await load();
      assert.deepEqual(errors, [failure]);
      assert.equal(writes.length, 1);
      assert.equal((writes[0] as { rootFolder: string }).rootFolder, "/workspace");
    }
  });
}
