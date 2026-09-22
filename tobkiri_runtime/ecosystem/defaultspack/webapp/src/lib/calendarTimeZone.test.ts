import test from "node:test";
import assert from "node:assert/strict";

import {
  CALENDAR_TIME_POLICY_VERSION,
  calendarTimeZoneOptions,
  calendarWallTimeFromInstant,
  formatCalendarResolvedInstant,
  resolveCalendarWallTime,
} from "./calendarTimeZone";

test("calendar policy has a stable version", () => {
  assert.equal(CALENDAR_TIME_POLICY_VERSION, "tobkiri.calendar-time.v1");
  assert.ok(calendarTimeZoneOptions("Pacific/Chatham").includes("Pacific/Chatham"));
});

test("resolved instants format with an explicit zone name", () => {
  const formatted = formatCalendarResolvedInstant("2026-07-20T00:00:00Z", "Asia/Tokyo");
  assert.match(formatted.local, /2026/);
  assert.equal(formatted.utc, "2026-07-20T00:00:00Z");
});

test("spring-forward gaps fail closed and suggest the next valid wall time", () => {
  const result = resolveCalendarWallTime("2026-03-08", "02:30", "America/New_York");
  assert.equal(result.status, "nonexistent");
  assert.equal(result.selected, null);
  assert.equal(result.suggestedDate, "2026-03-08");
  assert.equal(result.suggestedTime, "03:00");
});

test("fall-back duplicates require an earlier or later choice", () => {
  const unresolved = resolveCalendarWallTime("2026-11-01", "01:30", "America/New_York");
  const earlier = resolveCalendarWallTime("2026-11-01", "01:30", "America/New_York", "earlier");
  const later = resolveCalendarWallTime("2026-11-01", "01:30", "America/New_York", "later");

  assert.equal(unresolved.status, "ambiguous");
  assert.equal(unresolved.selected, null);
  assert.equal(earlier.selected?.iso, "2026-11-01T05:30:00Z");
  assert.equal(later.selected?.iso, "2026-11-01T06:30:00Z");
});

test("half-hour zones and UTC are resolved independently from the host zone", () => {
  assert.equal(
    resolveCalendarWallTime("2026-06-01", "09:00", "Asia/Kolkata").selected?.iso,
    "2026-06-01T03:30:00Z",
  );
  assert.equal(
    resolveCalendarWallTime("2028-02-29", "23:59", "UTC").selected?.iso,
    "2028-02-29T23:59:00Z",
  );
});

test("fixed instants render in the newly selected zone without reinterpretation", () => {
  assert.deepEqual(calendarWallTimeFromInstant("2026-06-01T00:00:00Z", "Asia/Tokyo"), {
    date: "2026-06-01",
    time: "09:00",
  });
  assert.deepEqual(calendarWallTimeFromInstant("2026-06-01T00:00:00Z", "America/Los_Angeles"), {
    date: "2026-05-31",
    time: "17:00",
  });
});

test("invalid zones and malformed dates remain non-actionable", () => {
  assert.equal(resolveCalendarWallTime("2026-01-01", "09:00", "Local/Guess").status, "invalid");
  assert.equal(resolveCalendarWallTime("2026-02-30", "09:00", "UTC").status, "invalid");
});
