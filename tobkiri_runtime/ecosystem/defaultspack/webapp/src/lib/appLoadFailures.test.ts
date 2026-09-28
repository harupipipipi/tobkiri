import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { createElement, type ComponentType, type ReactNode } from "react";
import { renderToStaticMarkup } from "react-dom/server";
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

function appFunction(name: string, bindings: Record<string, unknown>): unknown {
  let declaration: ts.FunctionDeclaration | undefined;
  const visit = (node: ts.Node) => {
    if (ts.isFunctionDeclaration(node) && node.name?.text === name) {
      assert.equal(declaration, undefined, `duplicate App function: ${name}`);
      declaration = node;
    }
    ts.forEachChild(node, visit);
  };
  visit(source);
  assert.ok(declaration, `missing App function: ${name}`);
  const javascript = ts.transpileModule(
    declaration.getText(source).replace(/^export\s+/, ""),
    {
      compilerOptions: {
        jsx: ts.JsxEmit.React,
        module: ts.ModuleKind.None,
        target: ts.ScriptTarget.ES2022,
      },
    },
  ).outputText;
  const scope = { React: { createElement }, ...bindings };
  return new Function(
    ...Object.keys(scope),
    `${javascript}\nreturn ${name};`,
  )(...Object.values(scope));
}

type NewConversationStageProps = {
  composerHomeTitle: string;
  error: string | null;
  isLaunching: boolean;
  onDismissError: () => void;
  onRetry?: () => void;
  renderComposer: (isCentered: boolean) => ReactNode;
};

type ErrorNoticeStubProps = {
  children?: ReactNode;
  errorIcon?: string;
  message: string;
};

function ErrorNoticeStub({ children, errorIcon, message }: ErrorNoticeStubProps): ReactNode {
  return createElement(
    "section",
    { "data-error-notice": errorIcon, role: "alert" },
    message,
    children,
  );
}

const NewConversationStage = appFunction("NewConversationStage", {
  ErrorNotice: ErrorNoticeStub,
  RefreshCw: () => null,
  X: () => null,
  cn: (...classes: Array<string | false | null | undefined>) => (
    classes.filter(Boolean).join(" ")
  ),
}) as ComponentType<NewConversationStageProps>;

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

test("an empty conversation keeps its composer visible after the first saved turn fails", () => {
  const markup = renderToStaticMarkup(
    createElement(NewConversationStage, {
      composerHomeTitle: "Welcome to Tobkiri",
      error: "The saved turn could not be completed.",
      isLaunching: false,
      onDismissError: () => undefined,
      onRetry: () => undefined,
      renderComposer: (isCentered) => createElement("textarea", {
        "data-centered-composer": String(isCentered),
      }),
    }),
  );

  assert.match(markup, /data-error-notice="chat"/);
  assert.match(markup, /The saved turn could not be completed\./);
  assert.match(markup, /data-centered-composer="true"/);
  assert.match(markup, />再試行</);
  assert.doesNotMatch(markup, /Welcome to Tobkiri/);
});

test("an empty conversation shows its greeting when no failed turn is present", () => {
  const markup = renderToStaticMarkup(
    createElement(NewConversationStage, {
      composerHomeTitle: "Welcome to Tobkiri",
      error: null,
      isLaunching: false,
      onDismissError: () => undefined,
      renderComposer: (isCentered) => createElement("textarea", {
        "data-centered-composer": String(isCentered),
      }),
    }),
  );

  assert.match(markup, /Welcome to Tobkiri/);
  assert.match(markup, /data-centered-composer="true"/);
  assert.doesNotMatch(markup, /data-error-notice="chat"/);
});

test("model profile changes refresh the settings catalog as well as profiles", async () => {
  const profiles: unknown[] = [];
  let catalogRefreshes = 0;
  const refreshModels = appExpression("refreshModels", {
    api: {
      listModelProfiles: async () => ({ profiles: ["saved-model"] }),
    },
    console: { error: () => undefined },
    disposed: false,
    refreshCatalog: async () => { catalogRefreshes += 1; },
    setModelProfiles: (value: unknown) => profiles.push(value),
  }) as () => void;

  refreshModels();
  await new Promise<void>((resolve) => setImmediate(resolve));

  assert.deepEqual(profiles, [["saved-model"]]);
  assert.equal(catalogRefreshes, 1);
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
