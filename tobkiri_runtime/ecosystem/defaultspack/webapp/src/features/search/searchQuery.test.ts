import test from "node:test";
import assert from "node:assert/strict";

import {
  applySearchSuggestion,
  parseSearchQuery,
  removeSearchToken,
  SEARCH_PROVIDER_ALIASES,
  suggestSearchFilters,
} from "./searchQuery";

test("plain search defaults to chats and keeps emails and unknown @ words literal", () => {
  const result = parseSearchQuery("  hello person@openai.com @unknown @model,  ");
  assert.equal(result.text, "hello person@openai.com @unknown @model,");
  assert.deepEqual(result.kinds, ["chat"]);
  assert.deepEqual(result.providerIds, []);
  assert.deepEqual(result.unknownTokens, ["@unknown", "@model,"]);
  assert.deepEqual(result.tokens, []);
  assert.equal(result.conflict, false);
});

test("filters parse anywhere with same-axis OR, cross-axis AND, aliases and deduplication", () => {
  const query = "天気 @MODEL fast @openrouter\n@widget @OpenAI @model @gemini @google";
  const result = parseSearchQuery(query);
  assert.equal(result.text, "天気 fast");
  assert.deepEqual(result.kinds, ["model", "widget"]);
  assert.deepEqual(result.providerIds, ["openrouter", "openai", "google"]);
  assert.equal(SEARCH_PROVIDER_ALIASES.gemini, "google");
  assert.equal(result.tokens.length, 7);
  for (const token of result.tokens) {
    assert.equal(query.slice(token.start, token.end), token.raw);
  }
  assert.equal(result.conflict, false);
});

test("provider shortcuts imply models unless explicit kinds are present", () => {
  assert.deepEqual(parseSearchQuery("@openrouter sonnet").kinds, ["model"]);
  assert.deepEqual(parseSearchQuery("@ollama llama").providerIds, ["ollama"]);
  assert.deepEqual(parseSearchQuery("@chat @openai release").kinds, ["chat"]);
});

test("presets apply mandatory intersections and preserve the exact connection scope", () => {
  const result = parseSearchQuery("@model @tool @openai @gemini fast", {
    kinds: ["model", "widget"],
    providerIds: ["google", "anthropic"],
    connectionId: "saved.OpenAI-key-1",
  });
  assert.deepEqual(result.kinds, ["model"]);
  assert.deepEqual(result.providerIds, ["google"]);
  assert.equal(result.connectionId, "saved.OpenAI-key-1");
  assert.equal(result.text, "fast");
  assert.equal(result.conflict, false);
  assert.deepEqual(parseSearchQuery("fast", { kinds: ["tool"] }).kinds, ["tool"]);
  assert.deepEqual(parseSearchQuery("fast", { providerIds: ["gemini"] }).providerIds, ["google"]);
  assert.deepEqual(parseSearchQuery("fast", { providerIds: ["google"] }).kinds, ["model"]);
});

test("empty intersections remain conflicts and never fall back to a wider scope", () => {
  const kindConflict = parseSearchQuery("@tool fast", { kinds: ["model"] });
  assert.deepEqual(kindConflict.kinds, []);
  assert.equal(kindConflict.conflict, true);
  const providerConflict = parseSearchQuery("@openrouter fast", { providerIds: ["openai"] });
  assert.deepEqual(providerConflict.providerIds, []);
  assert.equal(providerConflict.conflict, true);
  assert.equal(parseSearchQuery("@openai", { kinds: ["chat"] }).conflict, true);
  assert.equal(parseSearchQuery("fast", { kinds: [] }).conflict, true);
  assert.equal(parseSearchQuery("fast", { providerIds: [] }).conflict, true);
});

test("unknown tokens do not change a preset or silently become filters", () => {
  const result = parseSearchQuery("@custom @constructor @__proto__ query", { kinds: ["tool"] });
  assert.equal(result.text, "@custom @constructor @__proto__ query");
  assert.deepEqual(result.kinds, ["tool"]);
  assert.deepEqual(result.tokens, []);
  assert.deepEqual(result.unknownTokens, ["@custom", "@constructor", "@__proto__"]);
});

test("suggestions replace the caret token while retaining free text and earlier filters", () => {
  const query = "emoji 🐦 @chat find @op later";
  const caret = query.indexOf("@op") + 3;
  const suggestions = suggestSearchFilters(query, caret);
  assert.deepEqual(suggestions.map((item) => item.token), ["@openrouter", "@openai"]);
  const suggestion = suggestions.find((item) => item.token === "@openai")!;
  assert.equal(query.slice(suggestion.start, suggestion.end), "@op");
  assert.equal(applySearchSuggestion(query, suggestion), "emoji 🐦 @chat find @openai later");
  assert.deepEqual(suggestSearchFilters("mail@op"), []);
  assert.deepEqual(suggestSearchFilters("@model"), []);
  assert.deepEqual(suggestSearchFilters("@mo,"), []);
  assert.deepEqual(suggestSearchFilters("@mo", -1), []);
});

test("suggestions support a bare @ and replacing a whole token from its middle", () => {
  const all = suggestSearchFilters("@");
  const model = all.find((item) => item.token === "@model")!;
  assert.equal(applySearchSuggestion("@", model), "@model");
  const query = "@chat @openrouter tail";
  const inside = suggestSearchFilters(query, query.indexOf("@openrouter") + 3);
  const openai = inside.find((item) => item.token === "@openai")!;
  assert.equal(applySearchSuggestion(query, openai), "@chat @openai tail");
  assert.equal(applySearchSuggestion("changed plain text", openai), "changed plain text");
});

test("removing a filter keeps other occurrences and refuses stale or forged offsets", () => {
  const query = "find @model @openai @model fast";
  const token = parseSearchQuery(query).tokens[0];
  const removed = removeSearchToken(query, token);
  assert.equal(removed, "find @openai @model fast");
  assert.deepEqual(parseSearchQuery(removed).kinds, ["model"]);
  assert.equal(removeSearchToken(`new ${query}`, token), `new ${query}`);
  assert.equal(removeSearchToken(query, { ...token, value: "tool" }), query);
  assert.equal(removeSearchToken(query, { ...token, start: token.start + 1 }), query);
  const first = "@chat find";
  assert.equal(removeSearchToken(first, parseSearchQuery(first).tokens[0]), "find");
  const last = "find @chat";
  assert.equal(removeSearchToken(last, parseSearchQuery(last).tokens[0]), "find");
});
