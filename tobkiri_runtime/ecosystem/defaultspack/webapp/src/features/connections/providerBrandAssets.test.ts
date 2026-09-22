import assert from "node:assert/strict";
import test from "node:test";

import { providerBrandAsset } from "./providerBrandAssets";

test("account connections use vendored data images for supported brands", () => {
  for (const providerId of ["cloudflare", "google", "github", "codex"]) {
    const asset = providerBrandAsset(providerId);
    assert.match(asset ?? "", /^data:image\/svg\+xml,/);
    assert.match(decodeURIComponent(asset ?? ""), /<svg/);
  }
  assert.equal(providerBrandAsset("unknown"), null);
});
