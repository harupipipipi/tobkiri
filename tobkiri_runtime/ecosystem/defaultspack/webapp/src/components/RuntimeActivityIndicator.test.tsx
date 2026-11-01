import test from "node:test";
import assert from "node:assert/strict";
import { renderToStaticMarkup } from "react-dom/server";

import { RuntimeActivityIndicator } from "./RuntimeActivityIndicator";

test("runtime loading uses its supplied status with no manufactured duration or success", () => {
  const html = renderToStaticMarkup(<RuntimeActivityIndicator label="Waiting for runtime" />);
  assert.match(html, /aria-label="Waiting for runtime"/);
  assert.match(html, /rumi-loading-pixels/);
  assert.doesNotMatch(html, /is-animated|Completed|Success|[0-9]+s<\/span>/);
});

test("runtime loading elapsed time comes from the supplied start time", () => {
  const html = renderToStaticMarkup(<RuntimeActivityIndicator label="Tool running" startedAt={Date.now() - 65_000} />);
  assert.match(html, /1m 5s/);
  assert.match(html, /data-runtime-activity="running"/);
  assert.doesNotMatch(html, /https?:|video|iframe/);
});
