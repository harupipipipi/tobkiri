import test from "node:test";
import assert from "node:assert/strict";
import { createElement, Fragment } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { collectRailSlots, CustomizableSidebarRail, RailSlot } from "./CustomizableSidebarRail";

const noop = () => undefined;
const slot = (id: string, extra = {}) => createElement(RailSlot, {
  id, label: `Action ${id}`, category: "Activities", icon: createElement("span", null, "icon"),
  children: createElement("button", { title: `original-${id}`, onClick: noop }, id), ...extra,
});

test("slot extraction traverses fragments and nested arrays and retains original handlers", () => {
  const action = createElement("button", { onClick: noop }, "Run");
  const children = [slot("one"), createElement(Fragment, null,
    [slot("two"), slot("one"), null, false]),
  createElement(RailSlot, { id: "three", label: "Three", category: "Tools", icon: null, children: action }),
  slot("starred-tools"), slot("starred_tools"), slot("__starred_tools__")];
  const extracted = collectRailSlots(children);
  assert.deepEqual(extracted.map((item) => item.id), ["one", "two", "three"]);
  assert.equal(extracted[2].children, action);
  assert.equal(action.props.onClick, noop);
});

test("rail retains underlying original buttons, omits default hidden slots and offers keyboard reorder", () => {
  const html = renderToStaticMarkup(createElement(CustomizableSidebarRail, {
    children: [slot("one"), slot("hidden", { defaultVisible: false }), slot("unavailable", { available: false })],
  }));
  assert.match(html, /title="original-one"/);
  assert.doesNotMatch(html, /title="original-hidden"/);
  assert.doesNotMatch(html, /title="original-unavailable"/);
  assert.match(html, /aria-label="Action oneを並べ替え"/);
  assert.match(html, /Space.*Enter.*Home.*End.*Escape/);
  assert.match(html, /aria-live="polite"/);
  assert.match(html, /touch-action:none/);
});

test("Customize is permanent and outside the scrolling and reorderable list even with no slots", () => {
  const html = renderToStaticMarkup(createElement(CustomizableSidebarRail, { children: [] }));
  assert.match(html, /rumi-right-sidebar-rail-scroll.*min-h-0/);
  assert.match(html, /aria-label="カスタマイズ"/);
  assert.match(html, /aria-haspopup="dialog" aria-expanded="false"/);
  assert.doesNotMatch(html, /data-rail-slot-id/);
  assert.match(html, /<\/div><div class="flex shrink-0.*aria-label="カスタマイズ"/);
});

test("button keyboard policy applies to reorder handles and Customize without modifying action tabIndex", () => {
  const html = renderToStaticMarkup(createElement(CustomizableSidebarRail, {
    keyboardButtonNavigation: false, children: slot("one", {
      children: createElement("button", { tabIndex: 3, onClick: noop }, "Original"),
    }),
  }));
  assert.match(html, /tabindex="3"/);
  assert.match(html, /tabindex="-1" data-rail-reorder-handle/);
  assert.match(html, /aria-expanded="false" tabindex="-1"/);
});

test("RailSlot remains a transparent action container when rendered independently", () => {
  assert.equal(renderToStaticMarkup(slot("one")), '<button title="original-one">one</button>');
});
