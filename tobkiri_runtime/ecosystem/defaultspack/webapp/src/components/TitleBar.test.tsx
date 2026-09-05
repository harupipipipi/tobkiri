import assert from "node:assert/strict";
import test from "node:test";
import { renderToStaticMarkup } from "react-dom/server";

import { TitleBar, displayAppName, hasTauriNativeChrome } from "./TitleBar";

test("legacy Defaultspack product names display as Tobkiri", () => {
  for (const value of [undefined, "rumi DP", "Rumi Defaultspack", "Rumi Defaultspack v2"]) {
    assert.equal(displayAppName(value), "Tobkiri");
  }
  assert.equal(displayAppName("Third-party surface"), "Third-party surface");
});

test("browser title bar uses Tobkiri without custom window controls", () => {
  const html = renderToStaticMarkup(<TitleBar appName="rumi DP" />);

  assert.match(html, /Tobkiri/);
  assert.doesNotMatch(html, /Minimize window|Maximize window|Close window/);
});

test("Tauri native chrome suppresses the duplicate web title bar", () => {
  assert.equal(hasTauriNativeChrome({ __TAURI_INTERNALS__: {} }), true);
  assert.equal(hasTauriNativeChrome({}), false);
});
