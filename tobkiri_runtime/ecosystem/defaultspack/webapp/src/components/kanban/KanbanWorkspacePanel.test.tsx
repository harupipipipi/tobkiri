import test from "node:test";
import assert from "node:assert/strict";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import {
  KanbanWorkspacePanel,
  kanbanCardSummary,
  kanbanColumnSummary,
  kanbanMoveWithinColumnPayload,
  kanbanPriorityLabel,
} from "./KanbanWorkspacePanel";
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
    { column_id: "done", board_id: "board-1", title: "Done", position: 1, done: true, wip_limit: 1 },
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
  assert.match(html, /Move Fix composer before the previous card in To do/);
  assert.match(html, /Move Fix composer to another column/);
  assert.match(html, /Delete Fix composer/);
  assert.match(html, /Drag conversations from History/);
  assert.match(html, /Pointer drag is optional/);
  assert.match(html, /Start keyboard move for Fix composer/);
  assert.match(html, /Sync: Local card/);
  assert.match(html, /To do, card 1 of 1/);
  assert.match(html, /aria-expanded="false"/);
});

test("Kanban priority labels remain readable for known and custom priorities", () => {
  assert.equal(kanbanPriorityLabel("urgent"), "Urgent");
  assert.equal(kanbanPriorityLabel("normal"), "Normal");
  assert.equal(kanbanPriorityLabel("customer-blocked"), "customer-blocked");
});

test("Kanban summaries expose position, WIP, blocked, run, and checklist state", () => {
  assert.equal(
    kanbanColumnSummary(board.columns[1], 1, 2, 1),
    "Done, column 2 of 2, 1 card. WIP limit 1 reached.",
  );
  assert.equal(
    kanbanCardSummary(
      {
        ...board.cards[0],
        blocked_by: ["card-0"],
        agent_status: "running",
        due_at: "2026-08-28",
        checklist: [
          { id: "one", title: "One", done: true },
          { id: "two", title: "Two", done: false },
        ],
      },
      "To do",
      0,
      3,
    ),
    "Fix composer. To do, card 1 of 3. Priority Urgent. Blocked by 1 item. Run status running. Checklist 1 of 2 complete. Due 2026-08-28. Sync source local card.",
  );
});

test("Kanban non-drag moves bind relative order to exact card ids", () => {
  const cards = [
    { ...board.cards[0], card_id: "first", position: 0 },
    { ...board.cards[0], card_id: "second", position: 1 },
    { ...board.cards[0], card_id: "third", position: 2 },
  ];

  assert.deepEqual(
    kanbanMoveWithinColumnPayload(cards, "second", "before"),
    { column_id: "todo", before_card_id: "first", position: 0 },
  );
  assert.deepEqual(
    kanbanMoveWithinColumnPayload(cards, "second", "after"),
    { column_id: "todo", after_card_id: "third", position: 2 },
  );
  assert.equal(kanbanMoveWithinColumnPayload(cards, "first", "before"), null);
  assert.equal(kanbanMoveWithinColumnPayload(cards, "third", "after"), null);
});
