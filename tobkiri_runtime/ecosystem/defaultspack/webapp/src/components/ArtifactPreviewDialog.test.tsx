import test from "node:test";
import assert from "node:assert/strict";
import { renderToStaticMarkup } from "react-dom/server";
import { ArtifactPreviewDialog } from "./ArtifactPreviewDialog";

 test("artifact details start collapsed while preview and errors remain visible", () => {
  const html = renderToStaticMarkup(<ArtifactPreviewDialog onClose={() => {}} item={{
    kind: "tool", title: "Failed edit", content: "Error: permission denied",
    details: [{ label: "toolStepId", value: "call-".repeat(100) }, { label: "size", value: "10 KB" }],
  }} />);
  assert.match(html, /<details\b/);
  assert.doesNotMatch(html, /<details[^>]*\bopen(?:=|\s|>)/);
  assert.match(html, /<summary[^>]*>詳細…<\/summary>/);
  assert.match(html, /Error: permission denied/);
  assert.match(html, /break-all/);
  assert.match(html, /call-call-call/);
  assert.match(html, /10 KB/);
});

test("artifact without metadata has no empty disclosure", () => {
  const html = renderToStaticMarkup(<ArtifactPreviewDialog onClose={() => {}} item={{kind: "file",title: "memo.md",content: "memo"}} />);
  assert.doesNotMatch(html, /<details/);
  assert.match(html, /memo/);
});
