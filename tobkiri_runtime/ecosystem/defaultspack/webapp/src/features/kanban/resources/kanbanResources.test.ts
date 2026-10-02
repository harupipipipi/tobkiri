import assert from "node:assert/strict";
import test from "node:test";

import { kanbanResources } from "./kanbanResources";

function operationFromRequest(input: RequestInfo | URL): string {
  const marker = "/api/contracts/defaultspack/";
  const url = String(input);
  const operation = decodeURIComponent(url.slice(url.indexOf(marker) + marker.length));
  return operation;
}

const board = {
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
      return new Response(JSON.stringify({ status: "ok", data: { boards: [{
        board_id: "board-1", scope_type: "global", scope_id: "default", title: "All runs",
      }] } }));
    }
    if (operation === "GET /api/kanban/boards/board-1") {
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
    "GET /api/kanban/boards/board-1",
  ]);
});

test("Kanban resources bootstrap a missing board through the canonical route", async (context) => {
  const originalFetch = globalThis.fetch;
  context.after(() => { globalThis.fetch = originalFetch; });
  globalThis.fetch = async (input, init) => {
    assert.equal(operationFromRequest(input), "POST /api/kanban/boards/bootstrap");
    assert.equal(init?.method, "POST");
    assert.deepEqual(JSON.parse(String(init?.body)), {
      scope_type: "global", scope_id: "default", title: "All runs",
    });
    return new Response(JSON.stringify({ status: "ok", data: board }));
  };

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
    return new Response(JSON.stringify({ status: "ok", data: { boards: [board] } }));
  };

  const loaded = await kanbanResources.loadBoard({ type: "global", id: "default" });
  assert.deepEqual(loaded.board, board.board);
  assert.deepEqual(loaded.cards, board.cards);
});

test("Kanban resources move cards through the canonical move route", async (context) => {
  const originalFetch = globalThis.fetch;
  context.after(() => { globalThis.fetch = originalFetch; });
  globalThis.fetch = async (input, init) => {
    assert.equal(operationFromRequest(input), "POST /api/kanban/cards/card-1/move");
    assert.equal(init?.method, "POST");
    assert.deepEqual(JSON.parse(String(init?.body)), {
      board_id: "board-1", card_id: "card-1", column_id: "done",
    });
    return new Response(JSON.stringify({ status: "ok", data: {} }));
  };

  await kanbanResources.moveCard("board-1", "card-1", { column_id: "done" });
});
