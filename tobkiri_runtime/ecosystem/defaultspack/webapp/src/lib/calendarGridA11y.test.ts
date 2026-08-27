import assert from "node:assert/strict";
import test from "node:test";

import {
  addCalendarMonthsClamped,
  calendarDateAccessibleLabel,
  calendarGridNavigationDate,
} from "./calendarGridA11y";

test("calendar grid arrows and week keys follow the visible week model", () => {
  const wednesday = new Date(2026, 7, 12);
  assert.equal(calendarGridNavigationDate(wednesday, "ArrowLeft", "sunday")?.getDate(), 11);
  assert.equal(calendarGridNavigationDate(wednesday, "ArrowDown", "sunday")?.getDate(), 19);
  assert.equal(calendarGridNavigationDate(wednesday, "Home", "sunday")?.getDate(), 9);
  assert.equal(calendarGridNavigationDate(wednesday, "End", "sunday")?.getDate(), 15);
  assert.equal(calendarGridNavigationDate(wednesday, "Home", "monday")?.getDate(), 10);
  assert.equal(calendarGridNavigationDate(wednesday, "End", "monday")?.getDate(), 16);
});

test("calendar Page keys clamp month and year jumps", () => {
  const january31 = new Date(2025, 0, 31);
  assert.deepEqual(
    calendarGridNavigationDate(january31, "PageDown", "sunday")?.getDate(),
    28,
  );
  assert.equal(addCalendarMonthsClamped(new Date(2024, 1, 29), 12).getDate(), 28);
  assert.equal(
    calendarGridNavigationDate(new Date(2024, 1, 29), "PageDown", "sunday", true)?.getFullYear(),
    2025,
  );
  assert.equal(calendarGridNavigationDate(january31, "Enter", "sunday"), null);
});

test("calendar date labels expose coordinate, range, today, outside-month, and event state", () => {
  const label = calendarDateAccessibleLabel(new Date(2026, 7, 27), {
    eventCount: 3,
    isCurrentMonth: false,
    isRangeStart: true,
    isSelected: true,
    isToday: true,
  });
  assert.match(label, /2026年8月27日/);
  assert.match(label, /木曜日/);
  assert.match(label, /今日/);
  assert.match(label, /表示月の外/);
  assert.match(label, /選択範囲の開始/);
  assert.match(label, /予定3件/);

  const englishLabel = calendarDateAccessibleLabel(new Date(2026, 7, 27), {
    eventCount: 1,
    isCurrentMonth: true,
    isRangeEnd: true,
    isSelected: true,
    isToday: false,
  }, "en-US");
  assert.match(englishLabel, /Thursday/);
  assert.match(englishLabel, /range end/);
  assert.match(englishLabel, /1 event$/);
});
