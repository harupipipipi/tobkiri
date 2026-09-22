export const CALENDAR_TIME_POLICY_VERSION = "tobkiri.calendar-time.v1";

export type CalendarTimeMode = "floating" | "fixed";
export type CalendarDstResolution = "exact" | "earlier" | "later";

export type CalendarWallTimeCandidate = {
  iso: string;
  offset: string;
};

export type CalendarWallTimeResolution = {
  status: "exact" | "ambiguous" | "nonexistent" | "invalid";
  candidates: CalendarWallTimeCandidate[];
  selected: CalendarWallTimeCandidate | null;
  message: string;
  suggestedDate?: string;
  suggestedTime?: string;
};

type WallParts = {
  year: number;
  month: number;
  day: number;
  hour: number;
  minute: number;
};

const formatterCache = new Map<string, Intl.DateTimeFormat>();

function formatterFor(timeZone: string): Intl.DateTimeFormat {
  const cached = formatterCache.get(timeZone);
  if (cached) return cached;
  const formatter = new Intl.DateTimeFormat("en-CA", {
    timeZone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hourCycle: "h23",
  });
  formatter.format(0);
  formatterCache.set(timeZone, formatter);
  return formatter;
}

function parseWallParts(dateKey: string, time: string): WallParts | null {
  const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})$/.exec(`${dateKey}T${time}`);
  if (!match) return null;
  const parts = {
    year: Number(match[1]),
    month: Number(match[2]),
    day: Number(match[3]),
    hour: Number(match[4]),
    minute: Number(match[5]),
  };
  const probe = new Date(Date.UTC(parts.year, parts.month - 1, parts.day, parts.hour, parts.minute));
  if (
    probe.getUTCFullYear() !== parts.year
    || probe.getUTCMonth() + 1 !== parts.month
    || probe.getUTCDate() !== parts.day
    || parts.hour > 23
    || parts.minute > 59
  ) return null;
  return parts;
}

function partsAt(instant: number, timeZone: string): WallParts {
  const values: Record<string, number> = {};
  for (const part of formatterFor(timeZone).formatToParts(instant)) {
    if (part.type !== "literal") values[part.type] = Number(part.value);
  }
  return {
    year: values.year,
    month: values.month,
    day: values.day,
    hour: values.hour,
    minute: values.minute,
  };
}

function sameWallParts(left: WallParts, right: WallParts): boolean {
  return left.year === right.year
    && left.month === right.month
    && left.day === right.day
    && left.hour === right.hour
    && left.minute === right.minute;
}

function offsetAt(instant: number, timeZone: string): number {
  const parts = partsAt(instant, timeZone);
  const representedAsUtc = Date.UTC(
    parts.year,
    parts.month - 1,
    parts.day,
    parts.hour,
    parts.minute,
  );
  return representedAsUtc - Math.floor(instant / 60_000) * 60_000;
}

function offsetText(offsetMs: number): string {
  const totalMinutes = Math.round(offsetMs / 60_000);
  const sign = totalMinutes >= 0 ? "+" : "-";
  const absolute = Math.abs(totalMinutes);
  return `${sign}${String(Math.floor(absolute / 60)).padStart(2, "0")}:${String(absolute % 60).padStart(2, "0")}`;
}

function wallTimeCandidates(parts: WallParts, timeZone: string): CalendarWallTimeCandidate[] {
  const wallAsUtc = Date.UTC(parts.year, parts.month - 1, parts.day, parts.hour, parts.minute);
  const offsets = new Set<number>();
  for (let deltaHours = -36; deltaHours <= 36; deltaHours += 3) {
    offsets.add(offsetAt(wallAsUtc + deltaHours * 3_600_000, timeZone));
  }
  const candidates = new Map<number, CalendarWallTimeCandidate>();
  for (const offset of offsets) {
    const instant = wallAsUtc - offset;
    if (!sameWallParts(partsAt(instant, timeZone), parts)) continue;
    candidates.set(instant, {
      iso: new Date(instant).toISOString().replace(".000Z", "Z"),
      offset: offsetText(offset),
    });
  }
  return [...candidates.entries()]
    .sort(([left], [right]) => left - right)
    .map(([, candidate]) => candidate);
}

function addWallMinutes(parts: WallParts, minutes: number): WallParts {
  const next = new Date(Date.UTC(parts.year, parts.month - 1, parts.day, parts.hour, parts.minute + minutes));
  return {
    year: next.getUTCFullYear(),
    month: next.getUTCMonth() + 1,
    day: next.getUTCDate(),
    hour: next.getUTCHours(),
    minute: next.getUTCMinutes(),
  };
}

function dateKeyFromParts(parts: WallParts): string {
  return `${String(parts.year).padStart(4, "0")}-${String(parts.month).padStart(2, "0")}-${String(parts.day).padStart(2, "0")}`;
}

function timeFromParts(parts: WallParts): string {
  return `${String(parts.hour).padStart(2, "0")}:${String(parts.minute).padStart(2, "0")}`;
}

export function browserCalendarTimeZone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  } catch {
    return "UTC";
  }
}

export function calendarTimeZoneOptions(current = browserCalendarTimeZone()): string[] {
  return [...new Set([
    current,
    "UTC",
    "Asia/Tokyo",
    "Asia/Kolkata",
    "Australia/Adelaide",
    "Europe/London",
    "America/New_York",
    "America/Los_Angeles",
  ])];
}

export function resolveCalendarWallTime(
  dateKey: string,
  time: string,
  timeZone: string,
  dstResolution: CalendarDstResolution = "exact",
): CalendarWallTimeResolution {
  const parts = parseWallParts(dateKey, time);
  if (!parts) {
    return { status: "invalid", candidates: [], selected: null, message: "日付または時刻の形式が正しくありません。" };
  }
  try {
    formatterFor(timeZone);
  } catch {
    return { status: "invalid", candidates: [], selected: null, message: "有効なIANAタイムゾーンを選択してください。" };
  }
  const candidates = wallTimeCandidates(parts, timeZone);
  if (candidates.length === 1) {
    return { status: "exact", candidates, selected: candidates[0], message: "" };
  }
  if (candidates.length > 1) {
    const selected = dstResolution === "earlier"
      ? candidates[0]
      : dstResolution === "later"
        ? candidates[candidates.length - 1]
        : null;
    return {
      status: "ambiguous",
      candidates,
      selected,
      message: selected
        ? "夏時間の重複時刻を明示的に解決しました。"
        : "夏時間の終了により同じ時刻が2回あります。先の時刻か後の時刻を選んでください。",
    };
  }
  for (let minute = 1; minute <= 180; minute += 1) {
    const suggestion = addWallMinutes(parts, minute);
    if (wallTimeCandidates(suggestion, timeZone).length > 0) {
      return {
        status: "nonexistent",
        candidates: [],
        selected: null,
        message: "夏時間の開始により、この現地時刻は存在しません。時刻を変更してください。",
        suggestedDate: dateKeyFromParts(suggestion),
        suggestedTime: timeFromParts(suggestion),
      };
    }
  }
  return { status: "nonexistent", candidates: [], selected: null, message: "この現地時刻は存在しません。" };
}

export function calendarWallTimeFromInstant(iso: string, timeZone: string): { date: string; time: string } | null {
  const instant = Date.parse(iso);
  if (!Number.isFinite(instant)) return null;
  try {
    const parts = partsAt(instant, timeZone);
    return { date: dateKeyFromParts(parts), time: timeFromParts(parts) };
  } catch {
    return null;
  }
}

export function formatCalendarResolvedInstant(iso: string, timeZone: string): { local: string; utc: string } {
  const instant = Date.parse(iso);
  if (!Number.isFinite(instant)) return { local: "", utc: "" };
  const local = new Intl.DateTimeFormat("ja-JP", {
    timeZone,
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    timeZoneName: "short",
  }).format(instant);
  return { local, utc: new Date(instant).toISOString().replace(".000Z", "Z") };
}
