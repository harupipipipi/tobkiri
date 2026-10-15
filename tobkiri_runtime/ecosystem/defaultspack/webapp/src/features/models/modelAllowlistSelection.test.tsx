import assert from "node:assert/strict";
import test from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { ModelAllowlistField } from "../../renderers/SettingsModalRenderer";

test("allowlist keeps opaque stored selections and delegates additions to the shared picker", () => {
  const value = "opaque.saved-route\nopenrouter/google/gemma-4";
  const changes: string[] = [];
  const html = renderToStaticMarkup(createElement(ModelAllowlistField, {
    value, fallback: "stub/default", options: [], onChange: (next) => changes.push(next),
  }));
  assert.match(html, /opaque.saved-route/);
  assert.match(html, /openrouter\/google\/gemma-4/);
  assert.match(html, /data-model-search-picker="settings"/);
  assert.match(html, /モデルを追加/);
  assert.doesNotMatch(html, /stub\/default|<textarea/);
  assert.deepEqual(changes, []);
});
