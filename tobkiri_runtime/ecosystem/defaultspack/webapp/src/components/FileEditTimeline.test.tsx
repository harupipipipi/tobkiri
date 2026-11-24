import assert from "node:assert/strict";
import test from "node:test";
import { renderToStaticMarkup } from "react-dom/server";
import { FileEditTimeline, type FileEditTimelineEntry } from "./FileEditTimeline";

function entry(eventId: string, timestamp = 1_700_000_000_000, order = 1): FileEditTimelineEntry {
  return { eventId, fileId: `file-${eventId}`, path: `/project/${eventId}.tsx`, timestamp, order, addedLines: 7, deletedLines: 3 };
}

test("timeline orders edits by authoritative sequence, then time, without mutating input", () => {
  const entries = [entry("late", 1_699_000_000_000, 1_700_000_000_000_003), entry("second", 1_700_000_000_000, 1_700_000_000_000_002), entry("first", 1_700_000_001_000, 1_700_000_000_000_001)];
  const markup = renderToStaticMarkup(<FileEditTimeline entries={entries} />);
  assert.ok(markup.indexOf('data-event-id="first"') < markup.indexOf('data-event-id="second"'));
  assert.ok(markup.indexOf('data-event-id="second"') < markup.indexOf('data-event-id="late"'));
  assert.deepEqual(entries.map((item) => item.eventId), ["late", "second", "first"]);
  assert.match(markup, /datetime="2023-11-14T22:13:20.000Z"/i);
  assert.match(markup, /編集 #2/);
  assert.match(markup, /data-edit-order="1700000000000002"/);
  assert.match(markup, /title="順序 1700000000000002"/);
  assert.doesNotMatch(markup, /編集 #1700000000000002/);
});

test("static entries show filename and path without icons or unnecessary buttons", () => {
  const markup = renderToStaticMarkup(<FileEditTimeline entries={[entry("edited")]} />);
  assert.match(markup, /edited.tsx/); assert.match(markup, /\/project\/edited.tsx/);
  assert.doesNotMatch(markup, /<button|<svg|<img/);
  assert.match(markup, /text-red-400/); assert.match(markup, /text-blue-400/);
  assert.match(markup, /--/); assert.match(markup, /\+\+/);
});

test("open callback enables a filename button only when opening is supported", () => {
  const markup = renderToStaticMarkup(<FileEditTimeline entries={[entry("open")]} onOpen={() => undefined} />);
  assert.match(markup, /<button type="button"/);
  assert.match(markup, />open.tsx<\/button>/);
});

test("long paths wrap while edit metrics occupy a separate wrapping row", () => {
  const path = `/project/${"longdirectory".repeat(30)}/filename.tsx`;
  const markup = renderToStaticMarkup(<FileEditTimeline entries={[{ ...entry("long"), path }]} />);
  assert.ok(markup.includes(path));
  assert.match(markup, /break-all/);
  assert.match(markup, /flex min-w-0 flex-wrap gap-x-4 gap-y-2/);
  assert.match(markup, /shrink-0 items-baseline/);
  assert.doesNotMatch(markup, /truncate|class="(?:grid min-w-0|m-0 mt-1 break-all)[^"]*overflow-hidden/);
});

test("unknown counts remain unconfirmed and empty timelines are explicit", () => {
  const markup = renderToStaticMarkup(<FileEditTimeline entries={[{ ...entry("unknown"), addedLines: null, deletedLines: null, timestamp: NaN }]} />);
  assert.match(markup, /未確認/); assert.match(markup, /時刻未確認/);
  assert.doesNotMatch(markup, /NaN|Invalid Date/);
  assert.match(renderToStaticMarkup(<FileEditTimeline entries={[]} />), /確認済みのファイル編集はありません/);
});


test("matching sequence uses occurrence time and then event identity for stable order", () => {
  const markup = renderToStaticMarkup(<FileEditTimeline entries={[entry("z", 1_700_000_001_000, 7), entry("b", 1_700_000_000_000, 7), entry("a", 1_700_000_000_000, 7)]} />);
  assert.ok(markup.indexOf('data-event-id="a"') < markup.indexOf('data-event-id="b"'));
  assert.ok(markup.indexOf('data-event-id="b"') < markup.indexOf('data-event-id="z"'));
});
