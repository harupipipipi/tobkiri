import test from "node:test";
import assert from "node:assert/strict";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { SharedSearchTemplate } from "./SharedSearchTemplate";
import { confirmSearchSuggestion, createSearchQueryState, type SearchQueryState } from "./searchQueryState";
import type { SearchPreset } from "./searchQuery";

const render = (queryState: SearchQueryState, preset?: SearchPreset) => renderToStaticMarkup(
  createElement(SharedSearchTemplate, {
    queryState, onQueryStateChange: () => {}, preset,
    items: [{ key: "model", kind: "model", title: "Result Model", value: "model" }],
    onSelect: () => {},
  }),
);

test("an exact draft mention offers confirmation without active token styling or results", () => {
  const html = render(createSearchQueryState("@model"));
  assert.match(html, /aria-label="参照候補"/);
  assert.match(html, />@model<\/span>/);
  assert.doesNotMatch(html, /data-search-token=/);
  assert.doesNotMatch(html, /Result Model/);
});

test("a confirmed mention exposes results and styles only its inline occurrence", () => {
  const state = confirmSearchSuggestion(createSearchQueryState("@model"), {
    token: "@model", label: "モデル", start: 0, end: 6,
  }).state;
  const html = render(state);
  assert.match(html, /aria-label="検索結果"/);
  assert.match(html, /Result Model/);
  assert.match(html, /data-search-token="">@model<\/span>/);
  assert.doesNotMatch(html, /aria-label="参照候補"/);
});

test("hidden presets constrain candidates without adding visible preset chips", () => {
  const preset: SearchPreset = { kinds: ["model"], providerIds: ["openrouter"] };
  const empty = render(createSearchQueryState(), preset);
  assert.match(empty, /Result Model/);
  assert.doesNotMatch(empty, /@openrouter|data-search-token=/);
  const suggestions = render(createSearchQueryState("@"), preset);
  assert.match(suggestions, />@model<\/span>/);
  assert.match(suggestions, />@openrouter<\/span>/);
  assert.doesNotMatch(suggestions, />@chat<\/span>|>@openai<\/span>/);
  const incompatible = render(createSearchQueryState("@openai"), preset);
  assert.match(incompatible, /固定の検索条件とフィルターが一致しません/);
  assert.doesNotMatch(incompatible, /Result Model|data-search-token=/);
});

test("unknown mentions stay literal and never receive confirmed styling", () => {
  const html = render(createSearchQueryState("@unknown"));
  assert.match(html, /value="@unknown"/);
  assert.match(html, /Result Model/);
  assert.doesNotMatch(html, /data-search-token=|aria-label="参照候補"/);
});

test("result actions are sibling buttons and retain option semantics", () => {
  const html = renderToStaticMarkup(createElement(SharedSearchTemplate, {
    queryState: createSearchQueryState(), onQueryStateChange: () => {},
    items: [{ key: "model", kind: "model", title: "Result Model", value: "model" }],
    onSelect: () => {}, renderItemAction: () => createElement("button", { type: "button" }, "Pin"),
  }));
  assert.match(html, /data-search-result-row=""/);
  assert.match(html, /role="option" tabindex="-1" aria-selected="true"/);
  assert.match(html, /Result Model<\/span>[\s\S]*?<\/button><button type="button">Pin<\/button>/);
  const buttons = html.match(/<button\b[^>]*>[\s\S]*?<\/button>/g) ?? [];
  assert.equal(buttons.length, 2);
  for (const button of buttons) assert.equal((button.match(/<button\b/g) ?? []).length, 1);
});

test("result navigation retains keyed target across reorder without relaxing confirmation guards", async () => {
  const { readFileSync } = await import("node:fs");
  const source = readFileSync(new URL("./SharedSearchTemplate.tsx", import.meta.url), "utf8");
  assert.match(source, /items\.findIndex\(\(item\) => item\.key === activeItem\.key\)/);
  assert.match(source, /setActiveItem\(\{ queryState, key: items\[nextIndex\]\.key \}\)/);
  assert.match(source, /event\.nativeEvent\.isComposing \|\| event\.nativeEvent\.keyCode === 229/);
  assert.match(source, /event\.repeat && \(event\.key === "Enter"/);
  assert.match(source, /mentionConfirmationKey\([\s\S]+repeat: event\.repeat/);
  assert.match(source, /else if \(drafts\.length\) setDismissed\(false\)/);
});


test("bubbling result action Escape closes search while preserving input candidate Escape", async () => {
  const { readFileSync } = await import("node:fs");
  const source = readFileSync(new URL("./SharedSearchTemplate.tsx", import.meta.url), "utf8");
  assert.match(source, /return <div className="min-w-0" onKeyDown=\{\(event\) => \{[\s\S]*?event\.key !== "Escape" \|\| event\.defaultPrevented \|\| composingRef\.current[\s\S]*?event\.nativeEvent\.isComposing \|\| event\.nativeEvent\.keyCode === 229[\s\S]*?onEscape\?\.\(\)/);
  assert.match(source, /else if \(event\.key === "Escape"\) \{\s*event\.preventDefault\(\);\s*event\.stopPropagation\(\);\s*if \(suggestions\.length\) setDismissed\(true\);\s*else onEscape\?\.\(\)/);
});
