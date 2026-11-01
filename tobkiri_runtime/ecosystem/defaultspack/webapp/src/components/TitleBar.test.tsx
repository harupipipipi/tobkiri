import assert from "node:assert/strict";
import test from "node:test";
import { renderToStaticMarkup } from "react-dom/server";

import { TitleBar } from "./TitleBar";

test("the default renderer leaves window chrome to the browser or desktop shell", () => {
  assert.equal(renderToStaticMarkup(<TitleBar />), "");
  assert.equal(renderToStaticMarkup(<TitleBar appName="Tobkiri" appIcon="/icon.svg" />), "");
});
