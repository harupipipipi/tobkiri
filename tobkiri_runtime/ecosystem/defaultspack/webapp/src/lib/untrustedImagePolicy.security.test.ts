function routeKey(path: string): string {
  return `/${path}`;
}

import test from "node:test";
import assert from "node:assert/strict";

import {
  MAX_UNTRUSTED_IMAGE_URL_LENGTH,
  classifyUntrustedImageUrl,
  extractImageBlockUrl,
  imageBlockAttachmentId,
} from "./untrustedImagePolicy";

const APP_ORIGIN = "http://127.0.0.1:38766";

test("active, local-file, blob, and inline image schemes are blocked without parsing payloads", () => {
  for (const value of [
    "javascript:alert(document.cookie)",
    "file:///etc/passwd",
    "blob:https://tracker.example/id",
    "data:image/svg+xml,<svg onload=alert(1)>",
    "data:image/png;base64,iVBORw0KGgo=",
    "ftp://images.example/pixel.png",
  ]) {
    const result = classifyUntrustedImageUrl(value, { appOrigin: APP_ORIGIN });
    assert.equal(result.disposition, "blocked", value);
    assert.equal(result.normalizedUrl, "", value);
    assert.equal(result.reason, "unsafe-scheme", value);
  }
});

test("loopback and private IPv4 spellings cannot bypass normalization", () => {
  for (const value of [
    "http://127.0.0.1/pixel.gif",
    "http://127.1/pixel.gif",
    "http://2130706433/pixel.gif",
    "http://0177.0.0.1/pixel.gif",
    "http://0x7f000001/pixel.gif",
    "http://0.0.0.0/pixel.gif",
    "http://10.255.255.255/pixel.gif",
    "http://100.64.0.1/pixel.gif",
    "http://169.254.169.254/latest/meta-data",
    "http://172.31.255.255/pixel.gif",
    "http://192.168.1.1/pixel.gif",
    "http://198.18.0.1/pixel.gif",
    "http://224.0.0.1/pixel.gif",
  ]) {
    const result = classifyUntrustedImageUrl(value, { appOrigin: APP_ORIGIN });
    assert.equal(result.disposition, "blocked", value);
    assert.equal(result.reason, "local-or-private-network", value);
  }
});

test("IPv6 loopback, mapped IPv4, ULA, link-local, and multicast are blocked", () => {
  for (const value of [
    "http://[::]/pixel.gif",
    "http://[::1]/pixel.gif",
    "http://[::ffff:127.0.0.1]/pixel.gif",
    "http://[fc00::1]/pixel.gif",
    "http://[fdff::1]/pixel.gif",
    "http://[fe80::1]/pixel.gif",
    "http://[ff02::1]/pixel.gif",
  ]) {
    const result = classifyUntrustedImageUrl(value, { appOrigin: APP_ORIGIN });
    assert.equal(result.disposition, "blocked", value);
    assert.equal(result.reason, "local-or-private-network", value);
  }
});

test("hostname suffix checks are label-aware and do not overblock lookalikes", () => {
  for (const value of [
    "https://localhost/pixel.gif",
    "https://api.localhost/pixel.gif",
    "https://printer.local/pixel.gif",
    "https://LOCALHOST./pixel.gif",
  ]) {
    assert.equal(
      classifyUntrustedImageUrl(value, { appOrigin: APP_ORIGIN }).disposition,
      "blocked",
      value,
    );
  }

  for (const value of [
    "https://localhost.example/pixel.gif",
    "https://169.254.169.254.evil.example/pixel.gif",
    "https://notlocal/pixel.gif",
  ]) {
    assert.equal(
      classifyUntrustedImageUrl(value, { appOrigin: APP_ORIGIN }).disposition,
      "remote-consent",
      value,
    );
  }
});

test("embedded credentials, controls, malformed and oversized URLs fail closed", () => {
  const cases = [
    "https://user:secret@images.example/pixel.gif",
    "https://images.example/pixel.gif\nhttps://evil.example/",
    "https://[::1",
    "https://%zz.example/pixel.gif",
    `https://images.example/${"a".repeat(MAX_UNTRUSTED_IMAGE_URL_LENGTH)}`,
  ];
  for (const value of cases) {
    const result = classifyUntrustedImageUrl(value, { appOrigin: APP_ORIGIN });
    assert.equal(result.disposition, "blocked", value.slice(0, 100));
    assert.equal(result.normalizedUrl, "", value.slice(0, 100));
  }
});

test("remote tracking URLs are normalized but remain consent-gated", () => {
  const result = classifyUntrustedImageUrl(
    "  HTTPS://TRACKER.Example:443/a/../pixel.gif?uid=secret#fragment  ",
    { appOrigin: APP_ORIGIN },
  );
  assert.deepEqual(result, {
    disposition: "remote-consent",
    normalizedUrl: "https://tracker.example/pixel.gif?uid=secret#fragment",
    sourceLabel: "tracker.example",
  });
});

test("same-origin paths are not trusted without exact attachment identity", () => {
  for (const value of [
    `${APP_ORIGIN}/api/attachments/abcdefgh`,
    `${APP_ORIGIN}/api/attachments/abcdefgh-evil`,
    `${APP_ORIGIN}/api/media/attachments/abcdefgh`,
    `${APP_ORIGIN}/api/blobs/abcdefgh`,
    routeKey("api/attachments/abcdefgh"),
  ]) {
    assert.notEqual(
      classifyUntrustedImageUrl(value, { appOrigin: APP_ORIGIN }).disposition,
      "trusted-attachment",
      value,
    );
  }
});

test("trusted attachment identity requires valid id and exact same-origin path", () => {
  assert.equal(classifyUntrustedImageUrl(
    `${APP_ORIGIN}/api/attachments/attachment_123/image.png`,
    { appOrigin: APP_ORIGIN, attachmentId: "attachment_123", trustedAttachment: true },
  ).disposition, "trusted-attachment");

  assert.notEqual(classifyUntrustedImageUrl(
    `${APP_ORIGIN}/api/attachments/attachment_123/image.png`,
    { appOrigin: APP_ORIGIN, attachmentId: "attachment_123", trustedAttachment: false },
  ).disposition, "trusted-attachment", "an id without authoritative trust remains untrusted");

  for (const [value, attachmentId] of [
    [`${APP_ORIGIN}/api/attachments/attachment_1234`, "attachment_123"],
    [`${APP_ORIGIN}/api/attachments/attachment_123`, "../attachment_123"],
    ["https://tracker.example/api/attachments/attachment_123", "attachment_123"],
    [`${APP_ORIGIN}.evil.example/api/attachments/attachment_123`, "attachment_123"],
  ] as const) {
    assert.notEqual(
      classifyUntrustedImageUrl(value, {
        appOrigin: APP_ORIGIN,
        attachmentId,
        trustedAttachment: true,
      }).disposition,
      "trusted-attachment",
      value,
    );
  }
});

test("invalid app origins do not turn policy evaluation into a render crash", () => {
  assert.doesNotThrow(() => classifyUntrustedImageUrl("https://images.example/a.png", {
    appOrigin: "not an origin",
  }));
  assert.notEqual(
    classifyUntrustedImageUrl("https://images.example/a.png", { appOrigin: "not an origin" }).disposition,
    "trusted-attachment",
  );
});

test("block extraction ignores non-string URLs and only accepts bounded identity fields", () => {
  assert.equal(extractImageBlockUrl({ type: "image_url", url: { toString: () => "https://evil" } }), "");
  assert.equal(extractImageBlockUrl({ type: "image_url", image_url: { url: "https://images.example/a.png" } }), "https://images.example/a.png");
  assert.equal(imageBlockAttachmentId({ type: "image", attachment_id: 123 }), "");
  assert.equal(imageBlockAttachmentId({ type: "image", blobId: "  blob_123456  " }), "blob_123456");
});
