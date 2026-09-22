import test from "node:test";
import assert from "node:assert/strict";

import {
  approveAttachmentSecurityReview,
  attachmentMetadataOnly,
  attachmentNeedsSecurityReview,
  redactAttachmentSecurityFindings,
  scanAttachmentSecurity,
} from "./attachmentSecurity";
import type { AttachedFile } from "../renderers/types";

function attachment(name: string, content: string, truncated = false): AttachedFile {
  return { id: "attachment", name, size: content.length, content, truncated, type: "text/plain" };
}

test("detects high-risk filenames and common credential formats without copying values into findings", () => {
  const secretValues = [
    "AKIA1234567890ABCDEF",
    "ghp_abcdefghijklmnopqrstuvwxyz123456",
    "very-secret-database-password",
    "header-secret-value",
  ];
  const file = attachment(".env", [
    `AWS_ACCESS_KEY_ID=${secretValues[0]}`,
    `GITHUB_TOKEN=${secretValues[1]}`,
    `DATABASE_URL=postgres://user:${secretValues[2]}@db.internal/app`,
    `Authorization: Bearer ${secretValues[3]}`,
  ].join("\n"));

  const review = scanAttachmentSecurity(file);

  assert.equal(review.status, "required");
  for (const kind of ["high_risk_file", "aws_access_key", "provider_token", "connection_string", "authorization_header"]) {
    assert.ok(review.findings.some((finding) => finding.kind === kind), kind);
  }
  const serialized = JSON.stringify(review);
  for (const value of secretValues) assert.equal(serialized.includes(value), false);
});

test("redacts private keys, named secrets, cookies, and connection passwords", () => {
  const file = attachment("credentials.log", [
    "API_KEY=sk-abcdefghijklmnopqrstuvwxyz123456",
    "Cookie: session=top-secret-cookie; theme=dark",
    "redis://operator:redis-password@127.0.0.1:6379/0",
    "-----BEGIN PRIVATE KEY-----",
    "private-material-that-must-not-survive",
    "-----END PRIVATE KEY-----",
  ].join("\n"));
  const redacted = redactAttachmentSecurityFindings({ ...file, securityReview: scanAttachmentSecurity(file) });

  assert.equal(redacted.securityReview?.status, "redacted");
  assert.doesNotMatch(redacted.content ?? "", /top-secret-cookie|redis-password|private-material|sk-abc/);
  assert.match(redacted.content ?? "", /\[REDACTED\]/);
  assert.equal(attachmentNeedsSecurityReview(redacted), false);
});

test("selected redaction keeps review required while unselected sensitive content remains", () => {
  const file = attachment(
    "notes.txt",
    "GITHUB_TOKEN=ghp_abcdefghijklmnopqrstuvwxyz123456\nPASSWORD=another-secret-value",
  );
  const review = scanAttachmentSecurity(file);
  const providerFinding = review.findings.find((finding) => finding.kind === "provider_token");
  assert.ok(providerFinding);

  const partiallyRedacted = redactAttachmentSecurityFindings(
    { ...file, securityReview: review },
    [providerFinding.id],
  );

  assert.equal(partiallyRedacted.content?.includes("ghp_"), false);
  assert.equal(partiallyRedacted.content?.includes("another-secret-value"), true);
  assert.equal(partiallyRedacted.securityReview?.status, "required");
});

test("truncated content requires explicit review and approval is fingerprint-bound", () => {
  const file = attachment("notes.txt", "ordinary text", true);
  const reviewed = { ...file, securityReview: scanAttachmentSecurity(file) };
  assert.equal(attachmentNeedsSecurityReview(reviewed), true);
  const approved = approveAttachmentSecurityReview(reviewed);
  assert.equal(approved.securityReview?.status, "approved");
  assert.equal(approved.securityReview?.fingerprint, reviewed.securityReview?.fingerprint);
});

test("metadata-only removes file content and data URLs", () => {
  const file = { ...attachment(".env", "TOKEN=secret-value"), dataUrl: "data:text/plain,secret-value" };
  const metadata = attachmentMetadataOnly({ ...file, securityReview: scanAttachmentSecurity(file) });
  assert.equal(metadata.content, undefined);
  assert.equal(metadata.dataUrl, undefined);
  assert.equal(metadata.securityReview?.status, "metadata_only");
});

test("ordinary non-truncated text remains clear", () => {
  const review = scanAttachmentSecurity(attachment("notes.md", "Meeting at 10:00."));
  assert.equal(review.status, "clear");
  assert.deepEqual(review.findings, []);
});

test("flags hidden files, MIME-extension mismatches, and high-entropy candidates", () => {
  const hidden = scanAttachmentSecurity(attachment(".npmrc", "registry=https://registry.npmjs.org"));
  assert.ok(hidden.findings.some((finding) => finding.kind === "high_risk_file"));

  const spoofed = scanAttachmentSecurity({
    ...attachment("archive.zip", "not really a zip"),
    type: "text/plain",
  });
  assert.ok(spoofed.findings.some((finding) => finding.kind === "mime_mismatch"));

  const reverseSpoofed = scanAttachmentSecurity({
    ...attachment("notes.txt", "plain-looking"),
    type: "application/octet-stream",
  });
  assert.ok(reverseSpoofed.findings.some((finding) => finding.kind === "mime_mismatch"));

  const binaryControls = scanAttachmentSecurity(attachment("notes.txt", "prefix\u0000suffix"));
  assert.ok(binaryControls.findings.some((finding) => finding.kind === "mime_mismatch"));

  const entropy = scanAttachmentSecurity(attachment("notes.txt", "q7Vn2Lk9Pz4Xa8Mc1Re6Ty3Ui5Oo0WbH"));
  assert.ok(entropy.findings.some((finding) => finding.kind === "high_entropy_candidate"));
});

test("detects provider-specific credentials beyond GitHub and generic sk tokens", () => {
  const providerTokens = [
    ["AI", "zaSyA1234567890_abcdefghijklmnopqr"].join(""),
    ["xox", "b-123456789012-abcdefghijklmnopqrstuv"].join(""),
    ["npm", "_abcdefghijklmnopqrstuvwxyz123456"].join(""),
    ["sk_", "live_abcdefghijklmnopqrstuvwxyz"].join(""),
    ["hf", "_abcdefghijklmnopqrstuvwxyz123456"].join(""),
  ];
  for (const token of providerTokens) {
    const review = scanAttachmentSecurity(attachment("notes.txt", token));
    assert.ok(review.findings.some((finding) => finding.kind === "provider_token"), token.slice(0, 4));
    assert.equal(JSON.stringify(review).includes(token), false);
  }
});

test("fingerprint changes when reviewed metadata or a data URL changes", () => {
  const original = {
    id: "image",
    name: "scan.png",
    size: 4,
    type: "image/png",
    dataUrl: "data:image/png;base64,AAAA",
    truncated: false,
    source: "local_file" as const,
    sourcePath: "trusted/scan.png",
  };
  const fingerprint = scanAttachmentSecurity(original).fingerprint;
  for (const changed of [
    { ...original, name: "other.png" },
    { ...original, size: 5 },
    { ...original, type: "application/octet-stream" },
    { ...original, sourcePath: "other/scan.png" },
    { ...original, dataUrl: "data:image/png;base64,BBBB" },
  ]) {
    assert.notEqual(scanAttachmentSecurity(changed).fingerprint, fingerprint);
  }
});

test("unscanned data URLs require explicit review before dispatch", () => {
  const review = scanAttachmentSecurity({
    name: "notes.txt",
    size: 18,
    type: "text/plain",
    dataUrl: "data:text/plain;base64,c2VjcmV0LXZhbHVl",
    truncated: false,
  });

  assert.equal(review.status, "required");
  assert.ok(review.findings.some((finding) => finding.kind === "data_url_unscanned"));
  assert.equal(review.scannedCharacters, 0);
});

test("matches bounded user-configured literal patterns without exposing their values in findings", () => {
  const customValue = "internal-customer-marker";
  const review = scanAttachmentSecurity(
    attachment("notes.txt", `Reference: ${customValue}`),
    [customValue],
  );

  assert.ok(review.findings.some((finding) => finding.kind === "custom_pattern"));
  assert.equal(JSON.stringify(review).includes(customValue), false);
});
