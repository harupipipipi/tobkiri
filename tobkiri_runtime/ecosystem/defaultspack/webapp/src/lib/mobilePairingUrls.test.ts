import { describe, it } from "node:test";
import assert from "node:assert/strict";

import { buildMobilePairingBaseUrls, normalizeMobileBaseUrl } from "./mobilePairingUrls";

describe("mobilePairingUrls", () => {
  it("requires https origins by default for pairing QR base URLs", () => {
    const urls = buildMobilePairingBaseUrls([
      "http://localhost:8765",
      "http://127.0.0.1:8765",
      "http://192.168.1.44:8765",
      "https://rumi.example.com",
    ]);

    assert.deepEqual(urls, ["https://rumi.example.com"]);
  });

  it("can allow cleartext LAN origins for explicit debug flows", () => {
    const urls = buildMobilePairingBaseUrls([
      "http://localhost:8765",
      "http://127.0.0.1:8765",
      "http://192.168.1.44:8765",
    ], { allowCleartext: true });

    assert.deepEqual(urls, ["http://192.168.1.44:8765"]);
  });

  it("normalizes valid origins and rejects non-http values", () => {
    assert.equal(normalizeMobileBaseUrl("rumi.example.com/api/mobile/v1"), "https://rumi.example.com");
    assert.equal(
      normalizeMobileBaseUrl("192.168.1.44:8765/api/mobile/v1", { allowCleartext: true }),
      "http://192.168.1.44:8765",
    );
    assert.equal(normalizeMobileBaseUrl("file:///tmp/rumi"), "");
  });
});
