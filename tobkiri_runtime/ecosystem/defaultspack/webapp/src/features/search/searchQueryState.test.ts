import test from "node:test";
import assert from "node:assert/strict";
import {
  createSearchQueryState, updateSearchQueryState, confirmSearchSuggestion,
  parseConfirmedSearchQuery, getSearchQuerySuggestions, getDraftSearchTokens,
  mentionConfirmationKey,
} from "./searchQueryState";

const confirm = (value: string, token: string, start = 0) => confirmSearchSuggestion(
  createSearchQueryState(value), { token, label: token, start, end: start + value.slice(start).split(/\s/u)[0].length },
).state;

test("complete typed mentions remain drafts and selectable", () => {
  const state = createSearchQueryState("@model");
  assert.deepEqual(parseConfirmedSearchQuery(state).kinds, ["chat"]);
  assert.equal(parseConfirmedSearchQuery(state).text, "@model");
  assert.deepEqual(getSearchQuerySuggestions(state).map((item) => item.token), ["@model"]);
  assert.equal(getDraftSearchTokens(state).length, 1);
  const result = confirmSearchSuggestion(state, getSearchQuerySuggestions(state)[0]);
  assert.equal(result.state.value, "@model ");
  assert.equal(result.caret, 7);
  assert.deepEqual(parseConfirmedSearchQuery(result.state).kinds, ["model"]);
  assert.deepEqual(getSearchQuerySuggestions(result.state, 6), []);
});

test("edits shift surviving confirmations and invalidate edited words and boundaries", () => {
  const state = confirm("@model foo", "@model");
  const shifted = updateSearchQueryState(state, "hello @model foo");
  assert.equal(shifted.confirmed[0].start, 6);
  assert.equal(updateSearchQueryState(shifted, "@model foo").confirmed[0].start, 0);
  assert.deepEqual(updateSearchQueryState(state, "@models foo").confirmed, []);
  assert.deepEqual(updateSearchQueryState(state, "x@model foo").confirmed, []);
  assert.deepEqual(updateSearchQueryState(state, "@modelfoo").confirmed, []);
  assert.deepEqual(updateSearchQueryState(state, "foo").confirmed, []);
});

test("inserted duplicate remains draft and replacement confirms only selected word", () => {
  const original = confirm("@model foo", "@model");
  const state = updateSearchQueryState(original, "@model foo @model");
  assert.equal(state.confirmed.length, 1);
  assert.equal(getDraftSearchTokens(state).length, 1);
  const next = confirmSearchSuggestion(state, { token: "@openai", label: "", start: 11, end: 17 }).state;
  assert.equal(next.confirmed.length, 2);
  assert.deepEqual(parseConfirmedSearchQuery(next).providerIds, ["openai"]);
  assert.equal(parseConfirmedSearchQuery(next).text, "foo");
});

test("confirmed axis OR and immutable preset intersections retain draft and unknown text", () => {
  let state = confirm("@model @widget @openai @unknown", "@model");
  state = confirmSearchSuggestion(state, { token: "@widget", label: "", start: 7, end: 14 }).state;
  const preset = { kinds: ["tool" as const], providerIds: ["google"] };
  const parsed = parseConfirmedSearchQuery(state, preset);
  assert.equal(parsed.conflict, true);
  assert.deepEqual(parsed.kinds, []);
  assert.equal(parsed.text, "@openai @unknown");
  assert.deepEqual(preset, { kinds: ["tool"], providerIds: ["google"] });
  assert.deepEqual(parseConfirmedSearchQuery(state).kinds, ["model", "widget"]);
});

test("unknown or invalid ranges cannot be confirmed", () => {
  const state = createSearchQueryState("x@model @unknown");
  assert.equal(confirmSearchSuggestion(state, { token: "@model", label: "", start: 1, end: 7 }).state, state);
  assert.equal(confirmSearchSuggestion(state, { token: "@unknown", label: "", start: 8, end: 16 }).state, state);
});

test("keyboard confirmations exclude IME and reverse Tab", () => {
  assert.equal(mentionConfirmationKey({ key: "Enter" }), "enter");
  assert.equal(mentionConfirmationKey({ key: "Tab" }), "tab");
  assert.equal(mentionConfirmationKey({ key: "Tab", shiftKey: true }), null);
  assert.equal(mentionConfirmationKey({ key: "Enter", isComposing: true }), null);
  assert.equal(mentionConfirmationKey({ key: "Enter", keyCode: 229 }), null);
  assert.equal(mentionConfirmationKey({ key: "Escape" }), null);
});

test("duplicate deletion cannot transfer confirmation to indistinguishable draft", () => {
  const first = confirm("@model @model", "@model");
  const second = confirm("@model @model", "@model", 7);
  assert.deepEqual(updateSearchQueryState(first, "@model").confirmed, []);
  assert.deepEqual(updateSearchQueryState(second, "@model").confirmed, []);
  assert.equal(parseConfirmedSearchQuery(updateSearchQueryState(first, "@model")).text, "@model");
});

test("deleting and retyping a mention never restores its previous confirmation", () => {
  const state = confirm("@model foo", "@model");
  const deleted = updateSearchQueryState(state, "foo");
  const retyped = updateSearchQueryState(deleted, "@model foo");
  assert.deepEqual(retyped.confirmed, []);
  assert.equal(parseConfirmedSearchQuery(retyped).text, "@model foo");
  const replaced = updateSearchQueryState(state, "@model bar");
  assert.equal(replaced.confirmed.length, 1);
  const fullReplacement = updateSearchQueryState(state, "@openai bar");
  assert.deepEqual(fullReplacement.confirmed, []);
  assert.deepEqual(parseConfirmedSearchQuery(fullReplacement).providerIds, []);
});

test("middle insertion and Unicode offsets preserve only untouched occurrences", () => {
  const state = confirm("😀 日本語 @model tail", "@model", 7);
  const next = updateSearchQueryState(state, "😀 日本語 intro @model tail");
  const parsed = parseConfirmedSearchQuery(next);
  assert.equal(parsed.tokens.length, 1);
  assert.equal(parsed.tokens[0].start, 13);
  assert.equal(parsed.tokens[0].end, 19);
  assert.equal(next.value.slice(parsed.tokens[0].start, parsed.tokens[0].end), "@model");
  assert.equal(parsed.text, "😀 日本語 intro tail");
  assert.deepEqual(updateSearchQueryState(next, "😀 日本語 intro @models tail").confirmed, []);
});

test("whitespace boundaries accept separators but glued text invalidates confirmation", () => {
  const state = confirm("left @model right", "@model", 5);
  const tabbed = updateSearchQueryState(state, "left\t@model right");
  const separated = updateSearchQueryState(tabbed, "left\t@model\u3000right");
  assert.equal(separated.confirmed.length, 1);
  assert.equal(parseConfirmedSearchQuery(separated).text, "left right");
  assert.deepEqual(updateSearchQueryState(state, "left@model right").confirmed, []);
  assert.deepEqual(updateSearchQueryState(state, "left @modelright").confirmed, []);
});

test("forged confirmations must match every recognized token field", () => {
  const token = confirm("@gemini", "@gemini").confirmed[0];
  for (const invalid of [
    { ...token, raw: "@google" }, { ...token, value: "gemini" },
    { ...token, axis: "kind" as const }, { ...token, start: 1 },
    { ...token, end: 6 },
  ]) {
    const parsed = parseConfirmedSearchQuery({ value: "@gemini", confirmed: [invalid] });
    assert.deepEqual(parsed.tokens, []);
    assert.deepEqual(parsed.providerIds, []);
    assert.equal(parsed.text, "@gemini");
  }
  const unknown = parseConfirmedSearchQuery({
    value: "@unknown", confirmed: [{ ...token, raw: "@unknown", end: 8 }],
  });
  assert.deepEqual(unknown.tokens, []);
  assert.deepEqual(unknown.unknownTokens, ["@unknown"]);
  assert.deepEqual(parseConfirmedSearchQuery(confirm("@gemini", "@gemini")).providerIds, ["google"]);
});

test("hidden preset filters never become visible tokens or mutable query state", () => {
  const preset = Object.freeze({ kinds: ["model" as const], providerIds: ["openai"], connectionId: "local" });
  const state = createSearchQueryState("@model @openai");
  const before = JSON.stringify(state);
  const parsed = parseConfirmedSearchQuery(state, preset);
  assert.deepEqual(parsed.tokens, []);
  assert.equal(parsed.text, "@model @openai");
  assert.equal(parsed.connectionId, "local");
  assert.deepEqual(parsed.providerIds, ["openai"]);
  assert.equal(JSON.stringify(state), before);
});

test("both confirmation keys fail closed for every IME indicator", () => {
  for (const key of ["Enter", "Tab"]) {
    assert.equal(mentionConfirmationKey({ key, isComposing: true }), null);
    assert.equal(mentionConfirmationKey({ key, keyCode: 229 }), null);
    assert.equal(mentionConfirmationKey({ key, isComposing: true, keyCode: 229 }), null);
  }
});

test("native edit provenance distinguishes deleting either duplicate", () => {
  const first = confirm("@model @model", "@model");
  assert.deepEqual(updateSearchQueryState(first, "@model", { start: 0, end: 7 }).confirmed, []);
  assert.equal(updateSearchQueryState(first, "@model", { start: 6, end: 13 }).confirmed.length, 1);
  const second = confirm("@model @model", "@model", 7);
  const deletedFirst = updateSearchQueryState(second, "@model ", { start: 0, end: 7 });
  assert.equal(deletedFirst.confirmed.length, 1);
  assert.equal(deletedFirst.confirmed[0].start, 0);
});

test("same text replacement invalidates only overlapping confirmations", () => {
  const state = confirm("@model foo", "@model");
  assert.equal(updateSearchQueryState(state, state.value).confirmed.length, 1);
  assert.deepEqual(updateSearchQueryState(state, state.value, { start: 0, end: 6 }).confirmed, []);
  assert.equal(updateSearchQueryState(state, state.value, { start: 7, end: 10 }).confirmed.length, 1);
  assert.deepEqual(updateSearchQueryState(state, "else", { start: -1, end: 3 }).confirmed, []);
});

test("explicit suggestion replacement preserves a separately confirmed duplicate", () => {
  const first = confirm("@model @model", "@model");
  const result = confirmSearchSuggestion(first, { token: "@openai", label: "", start: 7, end: 13 });
  assert.equal(result.state.confirmed.length, 2);
  assert.deepEqual(parseConfirmedSearchQuery(result.state).providerIds, ["openai"]);
});


test("held confirmation keys cannot confirm again on repeat", () => {
  assert.equal(mentionConfirmationKey({ key: "Enter", repeat: true }), null);
  assert.equal(mentionConfirmationKey({ key: "Tab", repeat: true }), null);
});
