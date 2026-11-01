import assert from "node:assert/strict";
import test from "node:test";
import { viewportPopoverPosition } from "./viewportPopoverPosition";

const viewport = { left: 0, top: 0, width: 800, height: 600 };
test("flips above a bottom-edge anchor and shifts away from the right edge", () => {
  const position = viewportPopoverPosition({ left: 740, top: 550, bottom: 590 }, viewport);
  assert.equal(position.side, "top");
  assert.equal(position.left, 264);
  assert.equal(position.top, 184);
  assert.equal(position.maxHeight, 360);
});
test("constrains width and height for a small mobile viewport", () => {
  const position = viewportPopoverPosition({ left: 20, top: 140, bottom: 180 }, { left: 0, top: 0, width: 320, height: 240 });
  assert.equal(position.width, 288);
  assert.equal(position.left, 16);
  assert.equal(position.side, "top");
  assert.equal(position.maxHeight, 118);
  assert.equal(position.top, 16);
});
test("uses visual viewport offsets after virtual keyboard or pinch zoom", () => {
  const position = viewportPopoverPosition({ left: 50, top: 250, bottom: 280 }, { left: 100, top: 200, width: 400, height: 300 });
  assert.equal(position.left, 116);
  assert.equal(position.width, 368);
  assert.equal(position.top, 286);
  assert.equal(position.maxHeight, 198);
});
test("prefers above when it fits, but flips if there is insufficient room", () => {
  assert.equal(viewportPopoverPosition({ left: 20, top: 420, bottom: 450 }, viewport, 200, 100, "above").side, "top");
  assert.equal(viewportPopoverPosition({ left: 20, top: 30, bottom: 60 }, viewport, 200, 100, "above").side, "bottom");
});
test("keeps an offscreen anchor's panel within the viewport", () => {
  const position = viewportPopoverPosition({ left: -200, top: -80, bottom: -40 }, viewport);
  assert.equal(position.left, 16);
  assert.equal(position.top, 16);
  assert.equal(position.maxHeight, 360);
});
