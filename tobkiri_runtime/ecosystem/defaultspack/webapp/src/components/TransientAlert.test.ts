import assert from "node:assert/strict";
import test from "node:test";

import {
  ALERT_ANCHOR_GAP_PX,
  ALERT_AUTO_DISMISS_MS,
  alertPlacementForComposerPosition,
  alertPresentation,
} from "./TransientAlert";

test("transient alert template has a finite default lifetime", () => {
  assert.equal(ALERT_AUTO_DISMISS_MS, 3600);
});

test("transient alert template provides a distinct presentation for every tone", () => {
  const tones = ["success", "info", "warning", "error"] as const;
  const accents = tones.map((tone) => alertPresentation(tone).accentClassName);
  assert.equal(new Set(accents).size, tones.length);
  for (const tone of tones) {
    const presentation = alertPresentation(tone);
    assert.equal(typeof presentation.Icon, "object");
    assert.match(presentation.iconClassName, /text-/);
  }
});

test("transient alert follows the declarative composer position", () => {
  assert.equal(alertPlacementForComposerPosition("center"), "viewport-bottom");
  assert.equal(alertPlacementForComposerPosition("inline"), "viewport-bottom");
  assert.equal(alertPlacementForComposerPosition("bottom"), "above-composer");
  assert.equal(ALERT_ANCHOR_GAP_PX, 12);
});
