import assert from "node:assert/strict";
import test from "node:test";

import { kanbanResources, KanbanApiError, KanbanBoardMissingError } from "./kanbanResources";

function operationFromRequest(input: RequestInfo | URL): string {
  const marker = "/api/contracts/defaultspack/";
  const url = String(input);
  const operation = decodeURIComponent(url.slice(url.indexOf(marker) + marker.length));
  return operation;
}

const board = {
  revision: 7,
  board: {
    board_id: "board-1",
    scope_type: "global",
    scope_id: "default",
    title: "All runs",
  },
  columns: [{ column_id: "todo", board_id: "board-1", title: "To do", position: 0 }],
  cards: [{ card_id: "card-1", board_id: "board-1", column_id: "todo", position: 0, title: "Review Kanban" }],
  events: [],
};

test("Kanban resources load a scoped board through the canonical list and board routes", async (context) => {
  const originalFetch = globalThis.fetch;
  context.after(() => { globalThis.fetch = originalFetch; });
  const operations: string[] = [];
  globalThis.fetch = async (input) => {
    const operation = operationFromRequest(input);
    operations.push(operation);
    if (operation === "GET /api/kanban/boards?scope_type=global&scope_id=default") {
      return new Response(JSON.stringify({ status: "ok", data: { revision: 7, boards: [{
        board_id: "board-1", scope_type: "global", scope_id: "default", title: "All runs", expected_revision: 7,
      }] } }));
    }
    if (operation === "GET /api/kanban/board?board_id=board-1") {
      return new Response(JSON.stringify({ status: "ok", data: board }));
    }
    throw new Error(`Unexpected request: ${operation}`);
  };

  const loaded = await kanbanResources.loadBoard({ type: "global", id: "default" });
  assert.deepEqual(loaded.board, board.board);
  assert.deepEqual(loaded.columns, board.columns);
  assert.deepEqual(loaded.cards, board.cards);
  assert.deepEqual(operations, [
    "GET /api/kanban/boards?scope_type=global&scope_id=default",
    "GET /api/kanban/board?board_id=board-1",
  ]);
});

test("Kanban resources bootstrap a missing board through the canonical route", async (context) => {
  const originalFetch = globalThis.fetch;
  context.after(() => { globalThis.fetch = originalFetch; });
  globalThis.fetch = async (input, init) => {
    if (operationFromRequest(input) === "GET /api/kanban/boards?scope_type=global&scope_id=default") {
      return new Response(JSON.stringify({ status: "ok", data: { revision: 7, boards: [] } }));
    }
    assert.equal(operationFromRequest(input), "POST /api/kanban/boards/bootstrap");
    assert.equal(init?.method, "POST");
    assert.deepEqual(JSON.parse(String(init?.body)), {
      scope_type: "global", scope_id: "default", title: "All runs", expected_revision: 7,
    });
    return new Response(JSON.stringify({ status: "ok", data: board }));
  };

  await assert.rejects(kanbanResources.loadBoard({ type: "global", id: "default" }), KanbanBoardMissingError);
  const created = await kanbanResources.ensureBoard(
    { type: "global", id: "default" },
    "All runs",
  );
  assert.deepEqual(created.board, board.board);
  assert.deepEqual(created.columns, board.columns);
});

test("Kanban resources accept a board snapshot returned by the local adapter", async (context) => {
  const originalFetch = globalThis.fetch;
  context.after(() => { globalThis.fetch = originalFetch; });
  globalThis.fetch = async (input) => {
    assert.equal(
      operationFromRequest(input),
      "GET /api/kanban/boards?scope_type=global&scope_id=default",
    );
    return new Response(JSON.stringify({ status: "ok", data: { revision: 7, boards: [board] } }));
  };

  const loaded = await kanbanResources.loadBoard({ type: "global", id: "default" });
  assert.deepEqual(loaded.board, board.board);
  assert.deepEqual(loaded.cards, board.cards);
});

test("Kanban resources move cards through the canonical move route", async (context) => {
  const originalFetch = globalThis.fetch;
  context.after(() => { globalThis.fetch = originalFetch; });
  globalThis.fetch = async (input, init) => {
    if (operationFromRequest(input) === "GET /api/kanban/boards?scope_type=global&scope_id=default") {
      return new Response(JSON.stringify({ status: "ok", data: { revision: 7, boards: [board] } }));
    }
    assert.equal(operationFromRequest(input), "POST /api/kanban/cards/move");
    assert.equal(init?.method, "POST");
    assert.deepEqual(JSON.parse(String(init?.body)), {
      board_id: "board-1", card_id: "card-1", column_id: "done", expected_revision: 7,
    });
    return new Response(JSON.stringify({ status: "ok", data: {} }));
  };

  await kanbanResources.loadBoard({ type: "global", id: "default" });
  await kanbanResources.moveCard("board-1", "card-1", { column_id: "done" });
});

for (const status of [401, 404, 503]) {
  test(`Kanban endpoint ${status} is not a missing board and never probes alternate routes`, async (context) => {
    const originalFetch = globalThis.fetch;
    context.after(() => { globalThis.fetch = originalFetch; });
    const requests: string[] = [];
    globalThis.fetch = async (input) => {
      requests.push(operationFromRequest(input));
      return new Response(JSON.stringify({ status: "error", error: { message: "endpoint unavailable" } }), { status });
    };
    await assert.rejects(kanbanResources.loadBoard({ type: "global", id: "default" }), (error) => (
      error instanceof KanbanApiError && !(error instanceof KanbanBoardMissingError) && error.status === status
    ));
    assert.equal(requests.length, 1);
  });
}

for (const payload of [{}, { boards: [] }, { boards: [], revision: -1 }, { boards: null, revision: 0 }]) {
  test(`malformed Kanban list cannot initiate bootstrap: ${JSON.stringify(payload)}`, async (context) => {
    const originalFetch = globalThis.fetch;
    context.after(() => { globalThis.fetch = originalFetch; });
    globalThis.fetch = async () => new Response(JSON.stringify({ status: "ok", data: payload }));
    await assert.rejects(kanbanResources.loadBoard({ type: "global", id: "default" }), (error) => (
      error instanceof Error && !(error instanceof KanbanBoardMissingError)
    ));
  });
}

test("Kanban detail response must belong to the requested board and scope", async (context) => {
  const originalFetch = globalThis.fetch;
  context.after(() => { globalThis.fetch = originalFetch; });
  globalThis.fetch = async (input) => new Response(JSON.stringify({ status: "ok", data:
    operationFromRequest(input).startsWith("GET /api/kanban/boards?")
      ? { revision: 7, boards: [board.board] }
      : { ...board, board: { ...board.board, board_id: "other-board" } },
  }));
  await assert.rejects(kanbanResources.loadBoard({ type: "global", id: "default" }), /different board/);
});

test("delayed Kanban response cannot populate another Profile revision cache", async (context) => {
  const originalFetch = globalThis.fetch;
  const oldWindow = Object.getOwnPropertyDescriptor(globalThis, "window");
  const location = { pathname: "/p/profile-a/kanban" };
  Object.defineProperty(globalThis, "window", { configurable: true, value: { location } });
  context.after(() => {
    globalThis.fetch = originalFetch;
    if (oldWindow) Object.defineProperty(globalThis, "window", oldWindow);
    else Reflect.deleteProperty(globalThis, "window");
  });
  let release!: (response: Response) => void;
  globalThis.fetch = () => new Promise<Response>((resolve) => { release = resolve; });
  const pending = kanbanResources.loadBoard({ type: "global", id: "default" });
  location.pathname = "/p/profile-b/kanban";
  release(new Response(JSON.stringify({ status: "ok", data: { revision: 7, boards: [board] } })));
  await assert.rejects(pending, /Profile changed/);
  await assert.rejects(kanbanResources.moveCard("board-1", "card-1", { column_id: "done" }), /Reload/);
});
