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
