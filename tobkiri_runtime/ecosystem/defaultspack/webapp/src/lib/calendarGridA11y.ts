export type CalendarWeekStart = "sunday" | "monday";

function daysInMonth(year: number, month: number): number {
  return new Date(year, month + 1, 0).getDate();
}

/** Move to the same day in another month, clamping short months safely. */
export function addCalendarMonthsClamped(date: Date, offset: number): Date {
  const target = new Date(date.getFullYear(), date.getMonth() + offset, 1);
  target.setDate(Math.min(date.getDate(), daysInMonth(target.getFullYear(), target.getMonth())));
  return target;
}

/** Resolve the date targeted by the documented calendar-grid keys. */
export function calendarGridNavigationDate(
  date: Date,
  key: string,
  weekStart: CalendarWeekStart,
  yearJump = false,
): Date | null {
  const target = new Date(date.getFullYear(), date.getMonth(), date.getDate());
  if (key === "ArrowLeft") target.setDate(target.getDate() - 1);
  else if (key === "ArrowRight") target.setDate(target.getDate() + 1);
  else if (key === "ArrowUp") target.setDate(target.getDate() - 7);
  else if (key === "ArrowDown") target.setDate(target.getDate() + 7);
  else if (key === "Home" || key === "End") {
    const startIndex = weekStart === "monday" ? 1 : 0;
    const column = (target.getDay() - startIndex + 7) % 7;
    target.setDate(target.getDate() + (key === "Home" ? -column : 6 - column));
  } else if (key === "PageUp" || key === "PageDown") {
    const direction = key === "PageUp" ? -1 : 1;
    return addCalendarMonthsClamped(target, direction * (yearJump ? 12 : 1));
  } else {
    return null;
  }
  return target;
}

export type CalendarDateAccessibleState = {
  eventCount: number;
  isCurrentMonth: boolean;
  isRangeEnd?: boolean;
  isRangeStart?: boolean;
  isSelected: boolean;
  isToday: boolean;
};

/** Build a complete but bounded spoken label for one month-grid date. */
export function calendarDateAccessibleLabel(
  date: Date,
  state: CalendarDateAccessibleState,
  locale = "ja-JP",
): string {
  const parts = [new Intl.DateTimeFormat(locale, {
    year: "numeric",
    month: "long",
    day: "numeric",
    weekday: "long",
  }).format(date)];
  if (state.isToday) parts.push(locale.startsWith("ja") ? "今日" : "today");
  if (!state.isCurrentMonth) parts.push(locale.startsWith("ja") ? "表示月の外" : "outside displayed month");
  if (state.isRangeStart) parts.push(locale.startsWith("ja") ? "選択範囲の開始" : "range start");
  else if (state.isRangeEnd) parts.push(locale.startsWith("ja") ? "選択範囲の終了" : "range end");
  else if (state.isSelected) parts.push(locale.startsWith("ja") ? "選択範囲内" : "in selected range");
  parts.push(locale.startsWith("ja")
    ? `予定${state.eventCount}件`
    : `${state.eventCount} ${state.eventCount === 1 ? "event" : "events"}`);
  return parts.join("、");
}
