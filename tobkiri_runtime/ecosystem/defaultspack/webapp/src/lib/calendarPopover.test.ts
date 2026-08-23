import assert from "node:assert/strict";
import test from "node:test";

import {
  calendarEscapeAction,
  calendarOverlayPointerAction,
  calendarTimeMenuKeyAction,
  placeCalendarFloatingOverlay,
} from "./calendarPopover";

test("calendar time menu keyboard selection keeps focus ownership on the combobox", () => {
  const moved = calendarTimeMenuKeyAction({
    activeIndex: 0,
    isOpen: true,
    key: "ArrowDown",
    optionCount: 3,
    selectedIndex: 0,
  });
  assert.deepEqual(moved, { handled: true, nextActiveIndex: 1, nextOpen: true });
  assert.deepEqual(calendarTimeMenuKeyAction({
    activeIndex: moved.handled ? moved.nextActiveIndex : -1,
    isOpen: true,
    key: "Enter",
    optionCount: 3,
    selectedIndex: 0,
  }), { handled: true, nextActiveIndex: 1, nextOpen: false, selectedIndex: 1 });
});

test("calendar time menu supports wrapping, Home, End, Space, and Escape", () => {
  assert.deepEqual(calendarTimeMenuKeyAction({ activeIndex: 0, isOpen: true, key: "ArrowUp", optionCount: 4, selectedIndex: 0 }), {
    handled: true, nextActiveIndex: 3, nextOpen: true,
  });
  assert.deepEqual(calendarTimeMenuKeyAction({ activeIndex: 2, isOpen: true, key: "Home", optionCount: 4, selectedIndex: 0 }), {
    handled: true, nextActiveIndex: 0, nextOpen: true,
  });
  assert.deepEqual(calendarTimeMenuKeyAction({ activeIndex: 2, isOpen: true, key: "End", optionCount: 4, selectedIndex: 0 }), {
    handled: true, nextActiveIndex: 3, nextOpen: true,
  });
  assert.deepEqual(calendarTimeMenuKeyAction({ activeIndex: 2, isOpen: true, key: " ", optionCount: 4, selectedIndex: 0 }), {
    handled: true, nextActiveIndex: 2, nextOpen: false, selectedIndex: 2,
  });
  assert.deepEqual(calendarTimeMenuKeyAction({ activeIndex: 2, isOpen: true, key: "Escape", optionCount: 4, selectedIndex: 0 }), {
    handled: true, nextActiveIndex: 2, nextOpen: false,
  });
});

test("calendar outside pointer closes portaled surfaces but shell padding owns clicks", () => {
  assert.deepEqual(calendarOverlayPointerAction({
    hasActiveEditor: true,
    insideCalendar: false,
    insideEditorPopover: false,
    insideTimeInput: false,
    insideTimeMenu: false,
    isTimeMenuOpen: true,
  }), { closeEditor: true, closeTimeMenu: true });
  assert.deepEqual(calendarOverlayPointerAction({
    hasActiveEditor: true,
    insideCalendar: false,
    insideEditorPopover: true,
    insideTimeInput: false,
    insideTimeMenu: false,
    isTimeMenuOpen: true,
  }), { closeEditor: false, closeTimeMenu: true });
  assert.deepEqual(calendarOverlayPointerAction({
    hasActiveEditor: true,
    insideCalendar: false,
    insideEditorPopover: false,
    insideTimeInput: false,
    insideTimeMenu: true,
    isTimeMenuOpen: true,
  }), { closeEditor: false, closeTimeMenu: false });
  assert.deepEqual(calendarOverlayPointerAction({
    hasActiveEditor: true,
    higherPriorityOverlayOwnsEvent: true,
    insideCalendar: false,
    insideEditorPopover: false,
    insideTimeInput: false,
    insideTimeMenu: false,
    isTimeMenuOpen: true,
  }), { closeEditor: false, closeTimeMenu: false });
});

test("calendar Escape closes inward-out and yields to higher-priority layers", () => {
  assert.deepEqual(calendarEscapeAction({ hasActiveEditor: true, isTimeMenuOpen: true }), {
    closeEditor: false, closeTimeMenu: true,
  });
  assert.deepEqual(calendarEscapeAction({ hasActiveEditor: true, isTimeMenuOpen: false }), {
    closeEditor: true, closeTimeMenu: false,
  });
  assert.deepEqual(calendarEscapeAction({
    hasActiveEditor: true,
    isTimeMenuOpen: true,
    higherPriorityOverlayOwnsEvent: true,
  }), { closeEditor: false, closeTimeMenu: false });
});

test("calendar popover placement stays within small viewports on either side", () => {
  const top = placeCalendarFloatingOverlay(
    { bottom: 236, height: 40, left: 248, right: 316, top: 196, width: 68 },
    { height: 280, width: 320 },
    { preferredHeight: 460, preferredWidth: 320 },
  );
  assert.deepEqual({ align: top.align, left: top.left, side: top.side, width: top.width }, {
    align: "right", left: 12, side: "top", width: 296,
  });
  assert.ok(top.top >= 12);
  assert.ok(top.top + top.maxHeight <= 268);

  const bottom = placeCalendarFloatingOverlay(
    { bottom: 44, height: 32, left: 4, right: 72, top: 12, width: 68 },
    { height: 240, width: 280 },
    { preferredHeight: 140, preferredWidth: 210 },
  );
  assert.equal(bottom.side, "bottom");
  assert.ok(bottom.left >= 12);
  assert.ok(bottom.top + bottom.maxHeight <= 228);
});
