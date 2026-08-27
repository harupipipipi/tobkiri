import test from "node:test";
import assert from "node:assert/strict";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import {
  iconAttentionAnimationEnabled,
  normalizeIconAttention,
  WidgetAttentionIcon,
} from "./widgetAttention";


test("icon attention normalizes allowlisted info and danger pulses", () => {
  assert.deepEqual(normalizeIconAttention({
    active: true,
    tone: "info",
    effect: "pulse",
    accessible_label: "New activity",
  }), {
    active: true,
    tone: "info",
    effect: "pulse",
    accessibleLabel: "New activity",
  });
  assert.deepEqual(normalizeIconAttention({
    active: true,
    tone: "danger",
    effect: "pulse",
  }), {
    active: true,
    tone: "danger",
    effect: "pulse",
    accessibleLabel: "Widget needs attention",
  });
});


test("inactive malformed and unknown attention safely use the normal icon", () => {
  assert.equal(normalizeIconAttention({ active: false, tone: "info", effect: "pulse" }), null);
  assert.equal(normalizeIconAttention({ active: true, tone: "magenta", effect: "pulse" }), null);
  assert.equal(normalizeIconAttention({ active: true, tone: "info", effect: "blink" }), null);
  assert.equal(normalizeIconAttention("animate-ping text-red-500"), null);
  assert.equal(normalizeIconAttention({ active: true, tone: "info", effect: "pulse", className: "animate-ping" })?.effect, "pulse");
});


test("attention animation stops for reduced motion hidden surfaces and static effects", () => {
  const pulse = normalizeIconAttention({ active: true, tone: "info", effect: "pulse" });
  const staticState = normalizeIconAttention({ active: true, tone: "warning", effect: "none" });
  assert.equal(iconAttentionAnimationEnabled(pulse, { reducedMotion: false, visible: true }), true);
  assert.equal(iconAttentionAnimationEnabled(pulse, { reducedMotion: true, visible: true }), false);
  assert.equal(iconAttentionAnimationEnabled(pulse, { reducedMotion: false, visible: false }), false);
  assert.equal(iconAttentionAnimationEnabled(staticState, { reducedMotion: false, visible: true }), false);
});


test("attention wrapper exposes a status and a non-color cue without arbitrary styles", () => {
  const html = renderToStaticMarkup(createElement(
    WidgetAttentionIcon,
    {
      attention: {
        active: true,
        tone: "danger",
        effect: "pulse",
        accessible_label: "Approval required",
        className: "fixed inset-0 animate-ping",
        style: { color: "hotpink" },
      },
      motionEnabled: true,
      widgetId: "approval-widget",
    },
    createElement("svg", { "data-testid": "normal-icon" }),
  ));

  assert.match(html, /data-widget-icon-attention="active"/);
  assert.match(html, /data-widget-id="approval-widget"/);
  assert.match(html, /data-attention-tone="danger"/);
  assert.match(html, /data-attention-effect="pulse"/);
  assert.match(html, /aria-label="Approval required"/);
  assert.match(html, /rumi-widget-attention-pulse/);
  assert.match(html, /data-widget-attention-cue="dot"/);
  assert.doesNotMatch(html, /fixed inset-0|animate-ping|hotpink/);
});


test("unknown attention renders the existing icon unchanged", () => {
  const html = renderToStaticMarkup(createElement(
    WidgetAttentionIcon,
    { attention: { active: true, tone: "unknown", effect: "pulse" } },
    createElement("svg", { "data-testid": "normal-icon" }),
  ));
  assert.match(html, /data-testid="normal-icon"/);
  assert.doesNotMatch(html, /data-widget-icon-attention/);
});
