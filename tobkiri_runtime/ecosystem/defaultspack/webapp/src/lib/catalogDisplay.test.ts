import test from "node:test";
import assert from "node:assert/strict";

import { resolveCatalogDisplay, safeCatalogImagePath } from "./catalogDisplay";

test("tools and skills resolve the same canonical display metadata", () => {
  const ui = {
    icon: "shield-check",
    image: "/static/assets/catalog/release-review.png",
  };

  assert.deepEqual(resolveCatalogDisplay({ id: "tool.review", ui }, "composer"), ui);
  assert.deepEqual(resolveCatalogDisplay({ id: "skill.review", ui }, "composer"), ui);
  assert.deepEqual(resolveCatalogDisplay({ id: "tool.review", ui }, "sidebar"), ui);
  assert.deepEqual(resolveCatalogDisplay({ id: "skill.review", ui }, "sidebar"), ui);
});

test("surface-specific legacy tool icons fall back to canonical ui.icon", () => {
  const item = {
    id: "tool.git",
    ui: {
      icon: "wrench",
      item_icon: "file",
      composer_icon: "git",
      group_icon: "terminal",
    },
  };

  assert.equal(resolveCatalogDisplay(item, "composer").icon, "git");
  assert.equal(resolveCatalogDisplay(item, "sidebar").icon, "file");
  assert.equal(
    resolveCatalogDisplay({ id: "skill.legacy", metadata: { icon: "sparkles" } }, "composer").icon,
    "sparkles",
  );
});

test("catalog images fail closed unless they are same-origin static raster assets", () => {
  assert.equal(
    safeCatalogImagePath("/static/assets/catalog/review.webp"),
    "/static/assets/catalog/review.webp",
  );
  for (const unsafe of [
    "https://tracker.example/review.png",
    "data:image/png;base64,AAAA",
    "/static/assets/catalog/review.svg",
    "/static/../secret.png",
  ]) {
    assert.equal(safeCatalogImagePath(unsafe), undefined, unsafe);
  }
});
