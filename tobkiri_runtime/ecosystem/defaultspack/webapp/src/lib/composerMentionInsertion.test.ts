import test from "node:test";
import assert from "node:assert/strict";
import { insertAtMentionText } from "./composerMentionInsertion";

test("candidate replaces an entire mention when the cursor is in its middle", () => {
  const input = "Use @web_search then @files_read";
  const result = insertAtMentionText(input, input.indexOf("_search"), "Web Search");
  assert.equal(result.value, "Use @Web Search  then @files_read");
  assert.equal(result.cursor, "Use @Web Search ".length);
});

test("selected replacement respects the selection end and keeps surrounding prose", () => {
  const input = "Before @web search After @files_read";
  const result = insertAtMentionText(input, input.indexOf("web") + 2, "Web Search", [], input.indexOf(" After"));
  assert.equal(result.value, "Before @Web Search  After @files_read");
});

test("human-facing labels with spaces are replaced as one mention", () => {
  const input = "Use @Web Search then @Files";
  const result = insertAtMentionText(input, input.indexOf("Web") + 2, "Files", ["Web Search", "Files"]);
  assert.equal(result.value, "Use @Files  then @Files");
});

test("negative labels and trailing punctuation retain intent and prose", () => {
  const input = "Use @-Web Search. Then @Files";
  const result = insertAtMentionText(input, input.indexOf("Web") + 2, "-Files", ["Web Search", "Files"]);
  assert.equal(result.value, "Use @-Files. Then @Files");
  assert.equal(result.cursor, "Use @-Files".length);
});

test("Unicode file references preserve textarea offsets and neighboring mentions", () => {
  const input = "😀確認@README.md、次@files_read";
  const result = insertAtMentionText(input, input.indexOf("README") + 3, "日本語.md", ["README.md"]);
  assert.equal(result.value, "😀確認@日本語.md、次@files_read");
  assert.equal(result.cursor, "😀確認@日本語.md".length);
});

test("an unfinished mention leaves ordinary following text unchanged", () => {
  assert.equal(insertAtMentionText("Use @web ordinary words", 8, "Web Search").value, "Use @Web Search  ordinary words");
});

test("emails and URLs do not become active mentions", () => {
  for (const input of ["user@example.com", "https://example.com/@web"]) {
    const result = insertAtMentionText(input, input.length, "Files");
    assert.equal(result.value, `${input}@Files `);
  }
});
