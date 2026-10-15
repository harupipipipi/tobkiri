import assert from "node:assert/strict";
import test from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { readFileSync } from "node:fs";
import { ModelPinButton, modelPinKeyboardBlocked } from "./ModelPinButton";

test("pin action exposes state and matching Japanese accessible action with SVG", () => {
  for (const pinned of [false, true]) {
    const html = renderToStaticMarkup(createElement(ModelPinButton, {
      label: "Test model", pinned, onToggle: () => {},
    }));
    assert.match(html, new RegExp(`aria-pressed="${pinned}"`));
    assert.match(html, new RegExp(`aria-label="Test modelを${pinned ? "ピン留め解除" : "ピン留め"}"`));
    assert.match(html, /<svg[^>]+aria-hidden="true"/);
    assert.match(html, pinned ? /lucide-pin-off/ : /lucide-pin"/);
  }
});

test("disabled pin remains a disabled native button", () => {
  const html = renderToStaticMarkup(createElement(ModelPinButton, {
    label: "Test model", pinned: false, disabled: true, onToggle: () => {},
  }));
  assert.match(html, /disabled=""/);
  assert.match(html, /type="button"/);
});

test("pin event wiring keeps pointer focus and guards native keyboard activation", () => {
  const source = readFileSync(new URL("./ModelPinButton.tsx", import.meta.url), "utf8");
  assert.match(source, /onPointerDown[^\n]+preventDefault\(\)[^\n]+stopPropagation\(\)/);
  assert.match(source, /event\.nativeEvent\.isComposing/);
  assert.match(source, /keyCode: event\.nativeEvent\.keyCode, repeat: event\.repeat/);
  assert.match(source, /event\.key !== "Enter" && event\.key !== " "/);
  assert.match(source, /onKeyUp[\s\S]+event\.preventDefault\(\)/);
  assert.match(source, /onClick[\s\S]+event\.stopPropagation\(\)/);
  assert.match(source, /focus-visible:outline/);
});


test("IME release Enter is blocked before 50ms while normal Space remains usable", () => {
  for (const elapsed of [0, 1, 49, 49.999]) {
    assert.equal(modelPinKeyboardBlocked({ key: "Enter" }, elapsed), true);
    assert.equal(modelPinKeyboardBlocked({ key: " " }, elapsed), false);
  }
  for (const elapsed of [50, 51, Infinity]) {
    assert.equal(modelPinKeyboardBlocked({ key: "Enter" }, elapsed), false);
  }
  assert.equal(modelPinKeyboardBlocked({ key: "Enter" }), false);
});

test("both pin activation keys remain blocked for composition, legacy IME and repeat", () => {
  for (const key of ["Enter", " "]) {
    assert.equal(modelPinKeyboardBlocked({ key, isComposing: true }, 100), true);
    assert.equal(modelPinKeyboardBlocked({ key, keyCode: 229 }, 100), true);
    assert.equal(modelPinKeyboardBlocked({ key, repeat: true }, 100), true);
    assert.equal(modelPinKeyboardBlocked({ key, isComposing: false, keyCode: 13, repeat: false }, 100), false);
  }
});


test("pin keyboard handlers leave Escape and navigation keys available to the parent", () => {
  const source = readFileSync(new URL("./ModelPinButton.tsx", import.meta.url), "utf8");
  for (const handler of ["onKeyDown", "onKeyUp"]) {
    const body = source.slice(source.indexOf(`${handler}={(event) => {`));
    assert.match(body, /^onKey(?:Down|Up)=\{\(event\) => \{\s*if \(event\.key !== "Enter" && event\.key !== " "\) return;\s*event\.stopPropagation\(\)/);
  }
});
