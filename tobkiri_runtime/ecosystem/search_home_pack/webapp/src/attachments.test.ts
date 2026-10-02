import assert from "node:assert/strict";
import test from "node:test";

import {
  AttachmentPreparationError,
  attachmentPreparationMessage,
  attachmentSupportsAction,
  encodeSearchHomeAttachment,
  SEARCH_HOME_IMAGE_LIMIT_BYTES,
  SEARCH_HOME_TEXT_LIMIT_BYTES,
} from "./attachments";

test("encodes supported text files into the answer attachment contract", async () => {
  const file = new File(["alpha\nbeta"], "notes.md", { type: "text/markdown" });
  const attachment = await encodeSearchHomeAttachment(file);
  assert.equal(attachment.name, "notes.md");
  assert.equal(attachment.content, "alpha\nbeta");
  assert.equal(attachment.dataUrl, undefined);
});

test("encodes supported images as data URLs", async () => {
  const file = new File(
    [new Uint8Array([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a])],
    "pixel.png",
    { type: "image/png" },
  );
  const attachment = await encodeSearchHomeAttachment(file);
  assert.equal(attachment.dataUrl, "data:image/png;base64,iVBORw0KGgo=");
  assert.equal(attachment.content, undefined);
});

test("rejects unsupported and oversized attachments before showing a chip", async () => {
  await assert.rejects(
    encodeSearchHomeAttachment(new File([new Uint8Array(SEARCH_HOME_TEXT_LIMIT_BYTES + 1)], "large.txt", { type: "text/plain" })),
    /120 KB/,
  );
  await assert.rejects(
    encodeSearchHomeAttachment(new File([new Uint8Array(SEARCH_HOME_IMAGE_LIMIT_BYTES + 1)], "large.png", { type: "image/png" })),
    /5 MB/,
  );
  await assert.rejects(encodeSearchHomeAttachment(new File(["zip"], "archive.zip", { type: "application/zip" })), /not supported/);
  await assert.rejects(
    encodeSearchHomeAttachment(new File(["not png"], "pixel.png", { type: "image/png" })),
    /bytes do not match/,
  );
  await assert.rejects(
    encodeSearchHomeAttachment(new File(["plain"], "notes.txt", { type: "application/zip" })),
    /not supported/,
  );
  await assert.rejects(
    encodeSearchHomeAttachment(
      new File([new Uint8Array([0xff, 0xfe])], "notes.txt", { type: "text/plain" }),
    ),
    /valid UTF-8/,
  );
});

test("attachments only permit actions that can consume their context", () => {
  assert.equal(attachmentSupportsAction("smart"), true);
  assert.equal(attachmentSupportsAction("answer"), true);
  assert.equal(attachmentSupportsAction("google"), false);
  assert.equal(attachmentSupportsAction("open"), false);
});

test("attachment preparation errors expose only fixed user-facing copy", () => {
  assert.equal(
    attachmentPreparationMessage(new AttachmentPreparationError("INVALID_UTF8")),
    "Text and code files must contain valid UTF-8 text.",
  );
  assert.equal(
    attachmentPreparationMessage(new Error("secret provider traceback")),
    "The file could not be prepared. Choose a supported file and try again.",
  );
});
