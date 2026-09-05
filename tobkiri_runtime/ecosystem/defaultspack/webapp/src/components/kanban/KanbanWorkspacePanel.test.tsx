import test from "node:test";
import assert from "node:assert/strict";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { KanbanWorkspacePanel, kanbanPriorityLabel } from "./KanbanWorkspacePanel";
import type { KanbanBoardResponse } from "../../lib/api";

const board: KanbanBoardResponse = {
  board: {
    board_id: "board-1",
    scope_type: "global",
    scope_id: "default",
    title: "Product board",
  },
  columns: [
    { column_id: "todo", board_id: "board-1", title: "To do", position: 0 },
    { column_id: "done", board_id: "board-1", title: "Done", position: 1, done: true },
  ],
  cards: [
    { card_id: "card-1", board_id: "board-1", column_id: "todo", position: 0, title: "Fix composer", priority: "urgent" },
  ],
};

test("Kanban workspace renders host board data and history drop targets", () => {
  const html = renderToStaticMarkup(createElement(KanbanWorkspacePanel, {
    scope: { type: "global", id: "default" },
    scopeLabel: "All runs",
    initialData: board,
  }));

  assert.match(html, /Product board/);
  assert.match(html, /data-kanban-column-id="todo"/);
  assert.match(html, /Fix composer/);
  assert.match(html, /Move Fix composer right/);
  assert.match(html, /Delete Fix composer/);
  assert.match(html, /Drag conversations from History/);
});

test("Kanban priority labels remain readable for known and custom priorities", () => {
  assert.equal(kanbanPriorityLabel("urgent"), "Urgent");
  assert.equal(kanbanPriorityLabel("normal"), "Normal");
  assert.equal(kanbanPriorityLabel("customer-blocked"), "customer-blocked");
});
