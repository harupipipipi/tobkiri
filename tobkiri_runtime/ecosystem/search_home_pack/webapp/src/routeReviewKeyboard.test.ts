import assert from "node:assert/strict";
import test from "node:test";

import {
  resolveRouteReviewKeyboardAction,
  routeReviewKeyboardAction,
  type RouteReviewKeyboardEvent,
} from "./routeReviewKeyboard";
import type { RouteDecision } from "./routerTypes";

const reviewSurface = {} as EventTarget;

function keyboardEvent(
  overrides: Partial<RouteReviewKeyboardEvent> = {},
): RouteReviewKeyboardEvent {
  return {
    altKey: false,
    ctrlKey: false,
    currentTarget: reviewSurface,
    defaultPrevented: false,
    isComposing: false,
    key: "ArrowRight",
    metaKey: false,
    repeat: false,
    shiftKey: false,
    target: reviewSurface,
    ...overrides,
  };
}

test("focused review surface cycles selection and confirms in separate events", () => {
  assert.equal(routeReviewKeyboardAction(keyboardEvent(), true), "next");
  assert.equal(
    routeReviewKeyboardAction(keyboardEvent({ key: "ArrowLeft" }), true),
    "previous",
  );
  assert.equal(
    routeReviewKeyboardAction(keyboardEvent({ key: "Enter" }), true),
    "confirm",
  );
});

test("restored review remains inactive until explicitly enabled", () => {
  assert.equal(routeReviewKeyboardAction(keyboardEvent(), false), null);
  assert.equal(
    routeReviewKeyboardAction(keyboardEvent({ key: "Enter" }), false),
    null,
  );
});

test("IME, repeats, and assistive or browser modifier combinations are ignored", () => {
  for (const overrides of [
    { isComposing: true },
    { defaultPrevented: true },
    { repeat: true },
    { altKey: true },
    { ctrlKey: true },
    { metaKey: true },
    { shiftKey: true },
    { altKey: true, ctrlKey: true },
    { key: "Escape" },
  ]) {
    assert.equal(routeReviewKeyboardAction(keyboardEvent(overrides), true), null);
  }
});

test("candidate cycling never confirms, including the no-candidate fallback", () => {
  const decision: RouteDecision = {
    query: "review",
    target_url: "https://example.com/a",
    target_candidates: [
      { url: "https://example.com/a" },
      { url: "https://example.com/b" },
    ],
    selected_index: 0,
    fallback_url: "https://www.google.com/search?q=review",
  };
  assert.deepEqual(resolveRouteReviewKeyboardAction(decision, 0, "next"), {
    nextIndex: 1,
    shouldConfirm: false,
  });
  assert.deepEqual(resolveRouteReviewKeyboardAction(decision, 1, "previous"), {
    nextIndex: 0,
    shouldConfirm: false,
  });
  assert.deepEqual(
    resolveRouteReviewKeyboardAction(
      { ...decision, target_candidates: [], selected_index: -1 },
      -1,
      "next",
    ),
    { nextIndex: -1, shouldConfirm: false },
  );
});

test("confirmation is a separate action that preserves visible selection", () => {
  const decision: RouteDecision = {
    query: "review",
    target_url: "https://example.com/a",
    target_candidates: [{ url: "https://example.com/a" }],
    selected_index: 0,
    fallback_url: "https://www.google.com/search?q=review",
  };
  assert.deepEqual(resolveRouteReviewKeyboardAction(decision, 0, "confirm"), {
    nextIndex: 0,
    shouldConfirm: true,
  });
});

test("inputs, model and file controls, dialogs, results, contenteditable, and buttons are ignored", () => {
  for (const focusTarget of [
    { tagName: "INPUT", type: "search" },
    { tagName: "INPUT", type: "file" },
    { role: "combobox" },
    { role: "dialog" },
    { role: "article" },
    { isContentEditable: true },
    { tagName: "BUTTON" },
  ]) {
    assert.equal(
      routeReviewKeyboardAction(
        keyboardEvent({ target: focusTarget as unknown as EventTarget }),
        true,
      ),
      null,
    );
  }
});
