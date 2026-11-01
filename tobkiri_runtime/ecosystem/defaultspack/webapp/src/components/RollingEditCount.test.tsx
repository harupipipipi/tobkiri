import test from "node:test";
import assert from "node:assert/strict";
import { renderToStaticMarkup } from "react-dom/server";
import {
  editCountColumns, editCountText, RollingEditCount, RollingEditCountController,
} from "./RollingEditCount";

test("initial counts render immediately as fixed-width tabular digits with static accessible text", () => {
  const html = renderToStaticMarkup(<RollingEditCount value={109} />);
  assert.match(html, /data-edit-count="109"/);
  assert.match(html, /class="sr-only">109<\/span>/);
  assert.match(html, /aria-hidden="true"/);
  assert.match(html, /tabular-nums/);
  assert.match(html, /min-width:3ch/);
  assert.match(html, /width:1ch/);
  assert.doesNotMatch(html, /translateY\(-50%\)|aria-live|--|\+\+/);
  assert.equal(new RollingEditCountController(109).frame.animate, false);
});

test("unconfirmed counts remain distinct from a confirmed zero", () => {
  const unknown = renderToStaticMarkup(<RollingEditCount value={null} />);
  assert.match(unknown, /data-edit-count="unconfirmed"/);
  assert.match(unknown, /class="sr-only">未確認<\/span>/);
  assert.match(unknown, /—/);
  assert.doesNotMatch(unknown, />0<\/span>/);
  assert.match(renderToStaticMarkup(<RollingEditCount value={0} />), /data-edit-count="0"/);
  assert.equal(editCountText(Number.NaN), null);
  assert.equal(editCountText(Infinity), null);
});

test("only actual confirmed changes animate, with increasing and decreasing directions", () => {
  const controller = new RollingEditCountController(null);
  assert.equal(controller.update(7, true).animate, false);
  const same = controller.frame;
  assert.equal(controller.update(7, true), same);
  const up = controller.update(8, true);
  assert.equal(up.animate, true);
  assert.equal(up.direction, "up");
  assert.deepEqual(editCountColumns(up).map(({ from, to }) => [from, to]), [["7", "8"]]);
  const down = controller.update(3, true);
  assert.equal(down.animate, true);
  assert.equal(down.direction, "down");
  assert.equal(down.to, "3");
  assert.equal(controller.update(null, true).animate, false);
});

test("digit growth and shrinkage align from the right and settle to the exact target width", () => {
  const controller = new RollingEditCountController(99);
  const growth = controller.update(100, true);
  assert.deepEqual(editCountColumns(growth).map(({ from, to }) => [from, to]),
    [[" ", "1"], ["9", "0"], ["9", "0"]]);
  controller.settle(growth.revision);
  const shrink = controller.update(8, true);
  assert.deepEqual(editCountColumns(shrink).map(({ from, to }) => [from, to]),
    [["1", " "], ["0", " "], ["0", "8"]]);
  const settled = controller.settle(shrink.revision);
  assert.equal(settled.to, "8");
  assert.equal(editCountColumns(settled).length, 1);
});

test("rapid updates ignore stale completion and preserve the newest exact count", () => {
  const controller = new RollingEditCountController(2);
  const first = controller.update(20, true);
  const second = controller.update(203, true);
  assert.equal(controller.settle(first.revision), second);
  const third = controller.update(4, true);
  assert.equal(controller.settle(second.revision), third);
  assert.equal(controller.settle(third.revision).to, "4");
  assert.equal(controller.frame.animate, false);
});

test("reduced motion and hidden documents settle latest values and reject stale animation completions", () => {
  const controller = new RollingEditCountController(40);
  const moving = controller.update(41, true);
  const stopped = controller.stop();
  assert.equal(stopped.animate, false);
  assert.equal(stopped.from, "41");
  assert.equal(stopped.to, "41");
  assert.equal(controller.settle(moving.revision), stopped);
  const hiddenUpdate = controller.update(42, false);
  assert.equal(hiddenUpdate.animate, false);
  assert.equal(hiddenUpdate.from, "42");
  assert.equal(hiddenUpdate.to, "42");
  assert.equal(controller.update(43, true).animate, true);
});

test("receipt arrival reveals confirmed digits once from blank while SSR keeps the true count", () => {
  const html = renderToStaticMarkup(<RollingEditCount value={12} animateInitial />);
  assert.match(html, /class="sr-only">12<\/span>/);
  assert.match(html, /data-edit-count="12"/);
  assert.doesNotMatch(html, /translateY\(-50%\)/);
  const controller = new RollingEditCountController(12);
  const reveal = controller.revealInitial(true);
  assert.equal(reveal.animate, true);
  assert.equal(reveal.from, "");
  assert.equal(reveal.to, "12");
  assert.deepEqual(editCountColumns(reveal).map(({ from, to }) => [from, to]),
    [[" ", "1"], [" ", "2"]]);
  assert.equal(controller.revealInitial(true), reveal);
  const settled = controller.settle(reveal.revision);
  assert.equal(controller.revealInitial(true), settled);
  assert.equal(controller.update(12, true, true), settled);
});

test("initial reveal settles immediately when hidden or reduced-motion and never replays on visibility", () => {
  const controller = new RollingEditCountController(0);
  const initial = controller.frame;
  assert.equal(controller.revealInitial(false), initial);
  assert.equal(initial.to, "0");
  assert.equal(initial.animate, false);
  assert.equal(controller.revealInitial(true), initial);
});

test("receipt mode reveals unknown-to-confirmed digits from blank and keeps rapid updates exact", () => {
  const controller = new RollingEditCountController(null);
  assert.equal(controller.revealInitial(true).animate, false);
  const reveal = controller.update(5, true, true);
  assert.equal(reveal.from, "");
  assert.equal(reveal.to, "5");
  assert.equal(reveal.animate, true);
  const next = controller.update(3, true, true);
  assert.equal(next.from, "5");
  assert.equal(next.direction, "down");
  assert.equal(controller.settle(reveal.revision), next);
  assert.equal(controller.stop().to, "3");
  const hidden = controller.update(4, false, true);
  assert.equal(hidden.from, "4");
  assert.equal(hidden.animate, false);
});
